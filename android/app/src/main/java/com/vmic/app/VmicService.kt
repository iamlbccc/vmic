package com.vmic.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread
import kotlin.math.abs

class VmicService : Service() {

    private lateinit var nm: NotificationManager
    private var activeSocket: Socket? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private val retryLock = Object()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        nm = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_ID, "VMic stream", NotificationManager.IMPORTANCE_LOW)
            )
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            running.set(false)
            activeSocket?.let { runCatching { it.close() } }
            synchronized(retryLock) { retryLock.notifyAll() }
            return START_NOT_STICKY
        }
        val host = intent?.getStringExtra(EXTRA_HOST) ?: return START_NOT_STICKY
        val port = intent.getIntExtra(EXTRA_PORT, DEFAULT_PORT)
        if (!running.compareAndSet(false, true)) return START_NOT_STICKY
        startInForeground()
        thread(name = "vmic-stream") { streamLoop(host, port) }
        return START_NOT_STICKY
    }

    private fun startInForeground() {
        val n = buildNotification("Connecting...")
        if (Build.VERSION.SDK_INT >= 30) {
            startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIF_ID, n)
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, VmicService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val b = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL_ID)
        else Notification.Builder(this)
        return b.setSmallIcon(R.drawable.ic_mic)
            .setContentTitle("VMic")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .build()
    }

    private fun fmtDuration(totalSecs: Long): String {
        val h = totalSecs / 3600
        val m = totalSecs % 3600 / 60
        val s = totalSecs % 60
        return when {
            h > 0 -> "%dh %02dm %02ds".format(h, m, s)
            m > 0 -> "%dm %02ds".format(m, s)
            else -> "%ds".format(s)
        }
    }

    private fun publish(text: String) {
        statusText = text
        runCatching { nm.notify(NOTIF_ID, buildNotification(text)) }
    }

    private fun streamLoop(host: String, port: Int) {
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "vmic:stream")
            .apply {
                setReferenceCounted(false)
                acquire(WAKE_LOCK_TIMEOUT_MS)
            }
        wifiLock = (applicationContext.getSystemService(WIFI_SERVICE) as WifiManager)
            .createWifiLock(
                if (Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                else WifiManager.WIFI_MODE_FULL_HIGH_PERF,
                "vmic:wifi"
            )
            .apply { setReferenceCounted(false); acquire() }

        while (running.get() && !Thread.currentThread().isInterrupted) {
            var audioRecord: AudioRecord? = null
            try {
                val socket = Socket()
                socket.tcpNoDelay = true
                activeSocket = socket
                socket.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
                publish("Connected, starting capture ...")

                val out = socket.getOutputStream()
                val header = ByteBuffer.allocate(11).order(ByteOrder.LITTLE_ENDIAN)
                    .put(MAGIC)
                    .put(VERSION)
                    .putInt(SAMPLE_RATE)
                    .put(CHANNELS)
                    .put(BITS)
                    .array()
                out.write(header)
                out.flush()

                val minBuf = AudioRecord.getMinBufferSize(
                    SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT
                )
                if (minBuf <= 0) throw IllegalStateException("AudioRecord unsupported on this device")

                audioRecord = AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    maxOf(minBuf * 2, SAMPLE_RATE / 50 * 2 * 4)
                )
                if (audioRecord.state != AudioRecord.STATE_INITIALIZED) {
                    throw IllegalStateException("AudioRecord init failed (mic in use?)")
                }
                audioRecord.startRecording()
                connected = true

                val chunk = ByteArray(maxOf(minBuf / 2, SAMPLE_RATE / 50 * 2))
                var frames = 0L
                var peakRun = 0
                var lastPost = System.currentTimeMillis()

                publish("Streaming to $host:$port")
                while (running.get() && !Thread.currentThread().isInterrupted) {
                    val n = audioRecord.read(chunk, 0, chunk.size)
                    if (n > 0) {
                        out.write(chunk, 0, n)
                        frames += n / 2
                        var i = 0
                        while (i < n) {
                            val s = ((chunk[i + 1].toInt() and 0xFF) shl 8) or (chunk[i].toInt() and 0xFF)
                            val v = if (s >= 0x8000) abs(s - 0x10000) else s
                            if (v > peakRun) peakRun = v
                            i += 2
                        }
                        val now = System.currentTimeMillis()
                        if (now - lastPost >= 400) {
                            peakPct = peakRun * 100 / 32768
                            publish("Streaming ${fmtDuration(frames / SAMPLE_RATE)}")
                            peakRun = 0
                            lastPost = now
                        }
                    } else if (n < 0) {
                        throw IOException("AudioRecord read error $n")
                    }
                }
            } catch (e: Exception) {
                if (running.get()) publish("Error: ${e.message}")
            } finally {
                connected = false
                runCatching { audioRecord?.stop() }
                runCatching { audioRecord?.release() }
                activeSocket?.let { runCatching { it.close() } }
                activeSocket = null
            }

            if (!running.get()) break

            publish("Reconnecting in ${RETRY_DELAY_MS / 1000}s ...")
            synchronized(retryLock) {
                var waited = 0L
                while (waited < RETRY_DELAY_MS && running.get()) {
                    val t0 = System.currentTimeMillis()
                    runCatching { retryLock.wait(RETRY_DELAY_MS - waited) }
                    waited += System.currentTimeMillis() - t0
                }
            }
        }

        wakeLock?.let { runCatching { it.release() } }
        wakeLock = null
        wifiLock?.let { runCatching { it.release() } }
        wifiLock = null
        if (running.getAndSet(false)) statusText = "Stopped"
        stopForeground(true)
        stopSelf()
    }

    companion object {
        val running = AtomicBoolean(false)
        @Volatile var connected = false
        @Volatile var statusText = "Idle"
        @Volatile var peakPct = 0

        val MAGIC = "VMIC".toByteArray()
        const val VERSION: Byte = 1
        const val SAMPLE_RATE = 48000
        const val CHANNELS: Byte = 1
        const val BITS: Byte = 16
        const val DEFAULT_PORT = 18200
        const val EXTRA_HOST = "host"
        const val EXTRA_PORT = "port"
        const val ACTION_STOP = "com.vmic.app.STOP"
        const val CHANNEL_ID = "vmic-stream"
        const val NOTIF_ID = 1
        const val CONNECT_TIMEOUT_MS = 4000
        const val RETRY_DELAY_MS = 3000L
        const val WAKE_LOCK_TIMEOUT_MS = 4 * 60 * 60 * 1000L
    }
}
