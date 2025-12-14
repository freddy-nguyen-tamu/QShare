package com.example.qshare

import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.RequestBody.Companion.toRequestBody
import android.content.ContentResolver
import android.net.Uri
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Bundle
import android.provider.OpenableColumns
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch
import okhttp3.*
import okio.buffer
import okio.sink
import org.json.JSONObject
import java.io.InputStream
import java.net.InetAddress
import java.util.concurrent.TimeUnit

class MainActivity : ComponentActivity() {

    private lateinit var statusText: TextView
    private lateinit var listView: ListView
    private lateinit var refreshBtn: Button
    private lateinit var uploadBtn: Button

    private lateinit var nsdManager: NsdManager
    private var discoveryListener: NsdManager.DiscoveryListener? = null

    private var baseUrl: String? = null
    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    private val fileNames = mutableListOf<String>()
    private lateinit var adapter: ArrayAdapter<String>

    private val pickFile =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
            if (uri != null) uploadUri(uri)
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        setContentView(R.layout.activity_main)

        statusText = findViewById(R.id.statusText)
        listView = findViewById(R.id.listView)
        refreshBtn = findViewById(R.id.refreshBtn)
        uploadBtn = findViewById(R.id.uploadBtn)

        adapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, fileNames)
        listView.adapter = adapter

        nsdManager = getSystemService(NSD_SERVICE) as NsdManager

        refreshBtn.setOnClickListener {
            val url = baseUrl
            if (url == null) {
                toast("Not connected yet.")
            } else {
                fetchList()
            }
        }

        uploadBtn.setOnClickListener {
            // Let user pick any file
            pickFile.launch(arrayOf("*/*"))
        }

        listView.setOnItemClickListener { _, _, position, _ ->
            val url = baseUrl ?: return@setOnItemClickListener
            val name = fileNames[position]
            downloadFile(url, name)
        }

        startDiscovery()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopDiscovery()
    }

    private fun startDiscovery() {
        stopDiscovery()

        statusText.text = "Searching for QShare on Wi-Fi…"

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) { }

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                // We are looking for _qshare._tcp
                if (serviceInfo.serviceType == "_qshare._tcp.") {
                    nsdManager.resolveService(serviceInfo, resolveListener)
                }
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) { }

            override fun onDiscoveryStopped(serviceType: String) { }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                nsdManager.stopServiceDiscovery(this)
                runOnUiThread { statusText.text = "Discovery failed: $errorCode" }
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                nsdManager.stopServiceDiscovery(this)
            }
        }

        discoveryListener = listener
        nsdManager.discoverServices("_qshare._tcp.", NsdManager.PROTOCOL_DNS_SD, listener)
    }

    private fun stopDiscovery() {
        discoveryListener?.let {
            try { nsdManager.stopServiceDiscovery(it) } catch (_: Exception) {}
        }
        discoveryListener = null
    }

    private val resolveListener = object : NsdManager.ResolveListener {
        override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
            runOnUiThread { statusText.text = "Resolve failed: $errorCode" }
        }

        override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
            val host: InetAddress = serviceInfo.host
            val port: Int = serviceInfo.port
            val url = "http://${host.hostAddress}:$port"

            baseUrl = url

            runOnUiThread {
                statusText.text = "Connected: $url"
                toast("Found QShare: $url")
            }

            fetchList()
        }
    }

    private fun fetchList() {
        val url = baseUrl ?: return
        val req = Request.Builder().url("$url/api/list").get().build()

        GlobalScope.launch(Dispatchers.IO) {
            try {
                client.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) throw Exception("HTTP ${resp.code}")
                    val body = resp.body?.string() ?: "{}"
                    val json = JSONObject(body)
                    val files = json.getJSONArray("files")

                    val names = mutableListOf<String>()
                    for (i in 0 until files.length()) {
                        val obj = files.getJSONObject(i)
                        names.add(obj.getString("name"))
                    }

                    runOnUiThread {
                        fileNames.clear()
                        fileNames.addAll(names)
                        adapter.notifyDataSetChanged()
                        if (names.isEmpty()) toast("No files in shared folder.")
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { toast("List error: ${e.message}") }
            }
        }
    }

    private fun downloadFile(url: String, name: String) {
        val req = Request.Builder().url("$url/download/${name}").get().build()

        GlobalScope.launch(Dispatchers.IO) {
            try {
                client.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) throw Exception("HTTP ${resp.code}")
                    val body = resp.body ?: throw Exception("No body")

                    val outFile = java.io.File(cacheDir, name)
                    outFile.sink().buffer().use { sink ->
                        sink.writeAll(body.source())
                    }

                    runOnUiThread {
                        toast("Downloaded to app cache: ${outFile.name}")
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { toast("Download error: ${e.message}") }
            }
        }
    }

    private fun uploadUri(uri: Uri) {
        val url = baseUrl
        if (url == null) {
            toast("Not connected yet.")
            return
        }

        val resolver: ContentResolver = contentResolver
        val filename = queryName(resolver, uri) ?: "upload.bin"

        GlobalScope.launch(Dispatchers.IO) {
            try {
                val stream: InputStream = resolver.openInputStream(uri)
                    ?: throw Exception("Cannot open file")

                val fileBytes = stream.readBytes()

                val body = MultipartBody.Builder()
                    .setType(MultipartBody.FORM)
                    .addFormDataPart(
                        "file",
                        filename,
                        fileBytes.toRequestBody("application/octet-stream".toMediaTypeOrNull())
                    )
                    .build()

                val req = Request.Builder()
                    .url("$url/upload")
                    .post(body)
                    .build()

                client.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) throw Exception("HTTP ${resp.code}")
                    runOnUiThread {
                        toast("Uploaded: $filename")
                        fetchList()
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { toast("Upload error: ${e.message}") }
            }
        }
    }

    private fun queryName(resolver: ContentResolver, uri: Uri): String? {
        val cursor = resolver.query(uri, null, null, null, null) ?: return null
        return cursor.use {
            val nameIndex = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            it.moveToFirst()
            if (nameIndex >= 0) it.getString(nameIndex) else null
        }
    }

    private fun toast(msg: String) {
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
    }
}
