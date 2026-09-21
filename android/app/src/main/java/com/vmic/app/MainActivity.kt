package com.vmic.app

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import kotlin.concurrent.thread

class MainActivity : Activity() {

    private lateinit var hostEdit: EditText
    private lateinit var portEdit: EditText
    private lateinit var mainBtn: Button
    private lateinit var statusText: TextView
    private lateinit var chart: WaveChart

    private val ui = Handler(Looper.getMainLooper())
    private val poller = object : Runnable {
        override fun run() {
            refreshState()
            if (!isFinishing) ui.postDelayed(this, 400)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        hostEdit = findViewById(R.id.host)
        portEdit = findViewById(R.id.port)
        mainBtn = findViewById(R.id.startStop)
        statusText = findViewById(R.id.status)
        chart = findViewById(R.id.chart)
        loadPrefs()

        mainBtn.setOnClickListener {
            if (VmicService.running.get()) {
                startService(Intent(this, VmicService::class.java).setAction(VmicService.ACTION_STOP))
            } else if (hasMicPermission()) {
                startStreaming()
            } else {
                requestRuntimePermissions()
            }
        }
        findViewById<TextView>(R.id.testTone).setOnClickListener { playTestTone() }

        if (!VmicService.running.get()) {
            if (hasMicPermission()) startStreaming() else requestRuntimePermissions()
        }
        maybeRequestBatteryExemption()
    }

    private fun maybeRequestBatteryExemption() {
        val sp = getSharedPreferences(PREFS, MODE_PRIVATE)
        if (sp.getBoolean(PREF_ASKED_BATTERY, false)) return
        sp.edit().putBoolean(PREF_ASKED_BATTERY, true).apply()
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        runCatching {
            startActivity(
                Intent(
                    Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                    Uri.parse("package:$packageName")
                )
            )
        }
    }

    override fun onResume() {
        super.onResume()
        ui.post(poller)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(poller)
    }

    private fun hasMicPermission() =
        checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun requestRuntimePermissions() {
        val need = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) need.add(Manifest.permission.POST_NOTIFICATIONS)
        requestPermissions(need.toTypedArray(), REQ_PERMS)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_PERMS) return
        if (hasMicPermission()) {
            startStreaming()
        } else {
            statusText.text = getString(R.string.status_denied)
        }
    }

    private fun startStreaming() {
        val host = hostEdit.text.toString().trim()
        val port = portEdit.text.toString().trim().toIntOrNull() ?: VmicService.DEFAULT_PORT
        if (host.isEmpty()) {
            statusText.text = getString(R.string.status_no_host)
            return
        }
        savePrefs(host, port)
        val intent = Intent(this, VmicService::class.java)
            .putExtra(VmicService.EXTRA_HOST, host)
            .putExtra(VmicService.EXTRA_PORT, port)
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(intent) else startService(intent)
    }

    private fun loadPrefs() {
        val sp = getSharedPreferences(PREFS, MODE_PRIVATE)
        hostEdit.setText(sp.getString(PREF_HOST, DEFAULT_HOST))
        portEdit.setText(sp.getString(PREF_PORT, VmicService.DEFAULT_PORT.toString()))
    }

    private fun savePrefs(host: String, port: Int) {
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
            .putString(PREF_HOST, host)
            .putString(PREF_PORT, port.toString())
            .apply()
    }

    private fun batteryPct(): Int {
        val i = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return 0
        val level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
        return if (level >= 0 && scale > 0) level * 100 / scale else 0
    }

    private fun refreshState() {
        val on = VmicService.running.get()
        val conn = on && VmicService.connected
        val label: String
        val color: Int
        when {
            !on -> {
                label = getString(R.string.btn_start)
                color = getColor(R.color.accent)
            }
            !conn -> {
                label = getString(R.string.btn_auto)
                color = getColor(R.color.connecting)
            }
            else -> {
                label = getString(R.string.btn_stop)
                color = getColor(R.color.stop)
            }
        }
        mainBtn.text = label
        mainBtn.backgroundTintList = ColorStateList.valueOf(color)
        mainBtn.setTextColor(getColor(R.color.text_on_accent))
        statusText.text = VmicService.statusText
        chart.add(if (on) VmicService.peakPct.toFloat() else 0f, batteryPct().toFloat())
        hostEdit.isEnabled = !on
        portEdit.isEnabled = !on
        hostEdit.alpha = if (on) 0.5f else 1f
        portEdit.alpha = if (on) 0.5f else 1f
    }

    private fun playTestTone() {
        thread(name = "vmic-tone") {
            try {
                val rate = VmicService.SAMPLE_RATE
                val total = rate * 2
                val track = android.media.AudioTrack(
                    android.media.AudioAttributes.Builder()
                        .setUsage(android.media.AudioAttributes.USAGE_MEDIA)
                        .setContentType(android.media.AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build(),
                    android.media.AudioFormat.Builder()
                        .setEncoding(android.media.AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(rate)
                        .setChannelMask(android.media.AudioFormat.CHANNEL_OUT_MONO)
                        .build(),
                    rate / 5 * 2 * 2,
                    android.media.AudioTrack.MODE_STREAM,
                    0
                )
                track.play()
                val chunk = ShortArray(rate / 20)
                var written = 0
                while (written < total) {
                    for (i in chunk.indices) {
                        val v = (28000.0 * Math.sin(2.0 * Math.PI * 880.0 * (written + i) / rate)).toInt()
                        chunk[i] = v.toShort()
                    }
                    track.write(chunk, 0, chunk.size)
                    written += chunk.size
                }
                track.stop()
                track.release()
            } catch (_: Exception) {
            }
        }
    }

    companion object {
        const val REQ_PERMS = 1
        const val PREFS = "vmic"
        const val PREF_HOST = "host"
        const val PREF_PORT = "port"
        const val PREF_ASKED_BATTERY = "asked_battery"
        const val DEFAULT_HOST = ""
    }
}
