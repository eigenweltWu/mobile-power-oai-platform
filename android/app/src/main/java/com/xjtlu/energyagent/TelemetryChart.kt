package com.xjtlu.energyagent

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.util.AttributeSet
import android.view.View
import kotlin.math.max
import kotlin.math.min

/**
 * Minimal Canvas line chart showing the last ~60 s of one telemetry metric.
 * No charting library; null values are skipped (missing != 0).
 */
class TelemetryChart @JvmOverloads constructor(
    context: Context, attrs: AttributeSet? = null
) : View(context, attrs) {

    enum class Metric(val label: String, val unit: String, val pick: (DisplaySample) -> Double?) {
        POWER("功率", "W", { it.powerW }),
        CURRENT("电流", "µA", { it.currentUa }),
        VOLTAGE("电压", "mV", { it.voltageMv }),
        RSRP("SS-RSRP", "dBm", { it.rsrpDbm }),
        TEMPERATURE("温度", "°C", { it.temperatureC })
    }

    var metric: Metric = Metric.POWER
    var samples: List<DisplaySample> = emptyList()

    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.rgb(47, 93, 143); strokeWidth = 2f; style = Paint.Style.STROKE
    }
    private val gridPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.rgb(220, 226, 232); strokeWidth = 1f
    }
    private val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.rgb(90, 100, 110); textSize = 26f
    }
    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(30, 47, 93, 143); style = Paint.Style.FILL
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat(); val h = height.toFloat()
        val padLeft = 56f; val padRight = 16f; val padTop = 34f; val padBottom = 30f

        if (samples.isEmpty()) {
            canvas.drawText("等待采样…", padLeft, h / 2, labelPaint)
            return
        }

        // last 60 s window
        val latest = samples.last().elapsedNs
        val cutoff = latest - 60_000_000_000L
        val pts = samples.filter { it.elapsedNs >= cutoff }
            .mapNotNull { s -> metric.pick(s)?.let { v -> s.elapsedNs to v } }
        if (pts.size < 2) {
            canvas.drawText("数据不足", padLeft, h / 2, labelPaint)
            return
        }

        val tMin = pts.first().first; val tMax = pts.last().first
        val vAll = pts.map { it.second }
        var vMin = vAll.min()!!; var vMax = vAll.max()!!
        if (vMax - vMin < 1e-9) { vMin -= 1.0; vMax += 1.0 }
        val pad = (vMax - vMin) * 0.1
        vMin -= pad; vMax += pad

        fun x(t: Long) = padLeft + (t - tMin).toFloat() / (tMax - tMin).toFloat() * (w - padLeft - padRight)
        fun y(v: Double) = padTop + (1f - (v - vMin).toFloat() / (vMax - vMin).toFloat()) * (h - padTop - padBottom)

        // grid + labels
        for (i in 0..3) {
            val fy = padTop + (h - padTop - padBottom) * i / 3f
            canvas.drawLine(padLeft, fy, w - padRight, fy, gridPaint)
        }
        canvas.drawText(String.format("%.2f %s", vMax, metric.unit), padLeft, padTop - 8f, labelPaint)
        canvas.drawText(String.format("%.2f %s", vMin, metric.unit), padLeft, h - 6f, labelPaint)
        canvas.drawText(metric.label, w - padRight - 110f, padTop - 8f, labelPaint)

        // area fill + line
        val path = android.graphics.Path()
        var started = false
        for ((t, v) in pts) {
            val px = x(t); val py = y(v)
            if (started) path.lineTo(px, py) else { path.moveTo(px, py); started = true }
        }
        val fillPath = android.graphics.Path(path)
        fillPath.lineTo(x(pts.last().first), h - padBottom)
        fillPath.lineTo(x(pts.first().first), h - padBottom)
        fillPath.close()
        canvas.drawPath(fillPath, fillPaint)
        canvas.drawPath(path, linePaint)
    }
}
