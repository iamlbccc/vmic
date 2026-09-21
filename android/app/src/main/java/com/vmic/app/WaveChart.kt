package com.vmic.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.util.AttributeSet
import android.view.View

class WaveChart(context: Context, attrs: AttributeSet?) : View(context, attrs) {

    private val levels = FloatArray(CAP)
    private val batts = FloatArray(CAP)
    private var count = 0
    private var head = 0
    private var smooth = 0f

    private val colAccent = context.getColor(R.color.accent)
    private val colBatt = context.getColor(R.color.battery)

    private val levelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = colAccent
        style = Paint.Style.STROKE
        strokeWidth = 3f
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }
    private val battPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = colBatt
        style = Paint.Style.STROKE
        strokeWidth = 2f
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }
    private val grid = Paint().apply {
        color = context.getColor(R.color.level_track)
        strokeWidth = 1f
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = LEGEND_SP * resources.displayMetrics.scaledDensity
    }
    private val path = Path()

    fun add(level: Float, batt: Float) {
        val v = level.coerceIn(0f, 100f)
        smooth += (v - smooth) * SMOOTH_ALPHA
        val idx = (head + count) % CAP
        levels[idx] = smooth
        batts[idx] = batt.coerceIn(0f, 100f)
        if (count < CAP) count++ else head = (head + 1) % CAP
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        val pad = 6f
        val textH = LEGEND_SP * resources.displayMetrics.scaledDensity
        val top = textH + 6f

        textPaint.color = colAccent
        canvas.drawText("IN dB", pad + 2f, textH, textPaint)
        textPaint.color = colBatt
        canvas.drawText("BAT %", pad + 44f, textH, textPaint)

        var i = 1
        while (i <= 3) {
            val y = top + (h - pad - top) * i / 4f
            canvas.drawLine(pad, y, w - pad, y, grid)
            i++
        }
        if (count < 2) return
        drawSeries(canvas, levels, levelPaint, w, h, pad, top, true)
        drawSeries(canvas, batts, battPaint, w, h, pad, top, false)
    }

    private fun drawSeries(
        canvas: Canvas,
        data: FloatArray,
        paint: Paint,
        w: Float,
        h: Float,
        pad: Float,
        top: Float,
        dbScale: Boolean
    ) {
        path.reset()
        for (j in 0 until count) {
            val v = data[(head + j) % CAP]
            val u = if (dbScale) toDb(v) else v
            val x = w - pad - (w - 2 * pad) * (count - 1 - j) / (CAP - 1)
            val y = (h - pad - top) * (1f - u / 100f) + top
            if (j == 0) path.moveTo(x, y) else path.lineTo(x, y)
        }
        canvas.drawPath(path, paint)
    }

    private fun toDb(pct: Float): Float {
        if (pct <= 0.1f) return 0f
        val db = 20.0 * Math.log10((pct / 100f).toDouble())
        return (((db + 60.0) / 60.0) * 100.0).coerceIn(0.0, 100.0).toFloat()
    }

    companion object {
        const val CAP = 150
        const val LEGEND_SP = 9f
        const val SMOOTH_ALPHA = 0.3f
    }
}
