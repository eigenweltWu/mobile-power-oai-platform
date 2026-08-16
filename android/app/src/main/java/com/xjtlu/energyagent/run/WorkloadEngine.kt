package com.xjtlu.energyagent.run

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.TrafficStats
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress

/**
 * Generates UL workload (the key experimental load) bound to the cellular Network.
 * Byte accounting uses TrafficStats (app UID) as the authoritative counter.
 */
class WorkloadEngine(private val context: Context) {

    enum class Mode { IDLE, UL_CBR, UL_SATURATION, DL_SATURATION }

    @Volatile var mode: Mode = Mode.IDLE
        private set
    @Volatile var targetMbps: Double = 0.0
        private set

    private var job: Job? = null
    private var lastTxBytes: Long = 0
    private var lastCheckNs: Long = 0
    private val uid = android.os.Process.myUid()

    val appUidTxBytes: Long get() = TrafficStats.getUidTxBytes(uid)
    val appUidRxBytes: Long get() = TrafficStats.getUidRxBytes(uid)

    fun start(mode: Mode, targetMbps: Double, serverHost: String, serverPort: Int) {
        stop()
        this.mode = mode
        this.targetMbps = targetMbps
        if (mode == Mode.IDLE) return
        job = kotlinx.coroutines.GlobalScope.launch(Dispatchers.IO) {
            if (mode == Mode.DL_SATURATION) runDl() else runUdp(mode, targetMbps, serverHost, serverPort)
        }
    }

    fun stop() {
        job?.cancel()
        job = null
        mode = Mode.IDLE
    }

    /** DL: repeatedly download a large file from a public mirror near Suzhou
     *  (Alibaba Cloud, Shanghai) over the cellular Network, saturating the DL. */
    private suspend fun runDl() = withContext(Dispatchers.IO) {
        val network = findCellularNetwork() ?: return@withContext
        val url = java.net.URL(DL_URL)
        while (true) {
            try {
                val conn = network.openConnection(url) as java.net.HttpURLConnection
                conn.connectTimeout = 5000
                conn.readTimeout = 15000
                conn.setRequestProperty("Range", "bytes=0-")  // fresh full download each iteration
                val input = conn.inputStream
                val buf = ByteArray(65536)
                while (input.read(buf) != -1) { /* discard; bytes counted via TrafficStats */ }
                input.close()
                conn.disconnect()
            } catch (_: Exception) {
                delay(1000)
            }
        }
    }

    private suspend fun runUdp(mode: Mode, targetMbps: Double, host: String, port: Int) = withContext(Dispatchers.IO) {
        val network = findCellularNetwork() ?: return@withContext
        val socket = DatagramSocket()
        try {
            network.bindSocket(socket)
            socket.connect(InetAddress.getByName(host), port)
        } catch (e: Exception) {
            socket.close()
            return@withContext
        }

        val payloadSize = 1024
        val payload = ByteArray(payloadSize) { 0x55 }
        val packet = DatagramPacket(payload, payloadSize)

        try {
            if (mode == Mode.UL_CBR) {
                // target bits/sec -> packets/sec
                val targetBps = targetMbps * 1_000_000.0
                val pktPerSec = (targetBps / 8.0 / payloadSize).coerceAtLeast(1.0)
                val intervalMs = (1000.0 / pktPerSec).toLong().coerceAtLeast(1)
                while (true) {
                    val t0 = System.nanoTime()
                    socket.send(packet)
                    val elapsed = (System.nanoTime() - t0) / 1_000_000
                    val wait = intervalMs - elapsed
                    if (wait > 0) delay(wait)
                }
            } else {
                // saturation: send as fast as possible
                while (true) socket.send(packet)
            }
        } catch (_: Exception) {
        } finally {
            socket.close()
        }
    }

    /** Actual UL goodput (Mbps) from TrafficStats deltas. */
    fun actualUplinkMbps(): Double {
        val nowNs = System.nanoTime()
        val tx = TrafficStats.getUidTxBytes(uid)
        if (lastCheckNs == 0L) {
            lastTxBytes = tx
            lastCheckNs = nowNs
            return 0.0
        }
        val dt = (nowNs - lastCheckNs) / 1e9
        val rate = if (dt > 0) ((tx - lastTxBytes) * 8.0 / 1e6) / dt else 0.0
        lastTxBytes = tx
        lastCheckNs = nowNs
        return rate
    }

    private fun findCellularNetwork(): Network? {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        for (n in cm.allNetworks) {
            val caps = cm.getNetworkCapabilities(n) ?: continue
            if (caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) &&
                caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            ) return n
        }
        return cm.activeNetwork
    }

    companion object {
        // Public large-file mirror near Suzhou (Alibaba Cloud, Shanghai) for DL traffic.
        const val DL_URL =
            "https://mirrors.aliyun.com/ubuntu-releases/24.04/ubuntu-24.04.1-desktop-amd64.iso"
    }
}
