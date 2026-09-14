package com.illustro.sync

import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.DocumentsContract
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {

    private lateinit var urlEdit: EditText
    private lateinit var tokenEdit: EditText
    private lateinit var folderText: TextView
    private lateinit var statusText: TextView
    private lateinit var startBtn: Button

    private val handler = Handler(Looper.getMainLooper())
    private val poller = object : Runnable {
        override fun run() {
            statusText.text = SyncState.statusText()
            startBtn.isEnabled = !SyncState.running
            handler.postDelayed(this, 400)
        }
    }

    private val pickRequest = 4242

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val dp = resources.displayMetrics.density
        fun pad(v: Int) = (v * dp).toInt()
        val fg = 0xFFE8E9EE.toInt()
        val muted = 0xFF9AA0AD.toInt()
        val accent = 0xFF48DB80.toInt()

        val scroll = ScrollView(this).apply { setBackgroundColor(0xFF14151A.toInt()) }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad(20), pad(28), pad(20), pad(24))
        }
        scroll.addView(root, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        setContentView(scroll)

        fun label(text: String) {
            root.addView(TextView(this).apply {
                this.text = text; textSize = 13f; setTextColor(muted)
                setPadding(0, pad(12), 0, pad(4))
            })
        }

        root.addView(TextView(this).apply {
            text = "illustro sync"; textSize = 22f; setTextColor(accent); gravity = Gravity.START
        })
        root.addView(TextView(this).apply {
            text = "Upload images to your illustro server (one-way)"
            textSize = 13f; setTextColor(muted); setPadding(0, pad(2), 0, 0)
        })

        label("Server URL")
        urlEdit = EditText(this).apply {
            hint = "http://192.168.1.10:8000"; setText(Prefs.get(this@MainActivity, "url"))
            setTextColor(fg); textSize = 15f
        }
        root.addView(urlEdit)

        label("API token (leave empty if none)")
        tokenEdit = EditText(this).apply {
            setText(Prefs.get(this@MainActivity, "token")); setTextColor(fg); textSize = 15f
        }
        root.addView(tokenEdit)

        label("Folder")
        val pickBtn = Button(this).apply {
            text = "Pick folder…"; setAllCaps(false)
            setOnClickListener { pickFolder() }
        }
        root.addView(pickBtn)
        folderText = TextView(this).apply { text = "none"; setTextColor(fg); textSize = 13f; setPadding(0, pad(4), 0, 0) }
        root.addView(folderText)
        val savedUri = Prefs.get(this, "treeUri")
        if (savedUri.isNotEmpty()) folderText.text = Prefs.get(this, "folderLabel").ifEmpty { savedUri }

        label("Progress (updates live; continues with screen off)")
        startBtn = Button(this).apply {
            text = "Start upload"; setAllCaps(false); setBackgroundColor(accent); setTextColor(0xFF14151A.toInt())
            setOnClickListener { start() }
        }
        root.addView(startBtn)
        val stopBtn = Button(this).apply {
            text = "Stop"; setAllCaps(false)
            setOnClickListener {
                val i = Intent(this@MainActivity, UploadService::class.java)
                i.action = UploadService.ACTION_STOP
                startService(i)
            }
        }
        root.addView(stopBtn)

        statusText = TextView(this).apply { setTextColor(fg); textSize = 14f; setPadding(0, pad(10), 0, 0) }
        root.addView(statusText)

        handler.post(poller)
    }

    private fun pickFolder() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
        try {
            intent.putExtra(
                DocumentsContract.EXTRA_INITIAL_URI,
                DocumentsContract.buildDocumentUri("com.android.externalstorage.documents", "primary:DCIM")
            )
        } catch (_: Exception) {
        }
        startActivityForResult(Intent.createChooser(intent, "Choose image folder"), pickRequest)
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != pickRequest || resultCode != RESULT_OK) return
        val uri: Uri = data?.data ?: return
        try {
            contentResolver.takePersistableUriPermission(
                uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
            )
        } catch (_: Exception) {
        }
        Prefs.put(this, "treeUri", uri.toString())
        val label = describe(uri)
        Prefs.put(this, "folderLabel", label)
        folderText.text = label
    }

    private fun describe(uri: Uri): String = try {
        val docId = DocumentsContract.getTreeDocumentId(uri)   // e.g. "primary:Pictures/anime"
        if (docId.startsWith("primary:")) "/storage/emulated/0/${docId.removePrefix("primary:")}" else docId
    } catch (_: Exception) {
        uri.toString()
    }

    private fun start() {
        val url = urlEdit.text.toString().trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            toast("Enter the server URL, e.g. http://192.168.1.10:8000"); return
        }
        if (Prefs.get(this, "treeUri").isEmpty()) { toast("Pick a folder first"); return }

        Prefs.put(this, "url", url)
        Prefs.put(this, "token", tokenEdit.text.toString())

        val wanted = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33) {
            if (checkSelfPermission(android.Manifest.permission.READ_MEDIA_IMAGES) != PackageManager.PERMISSION_GRANTED)
                wanted.add(android.Manifest.permission.READ_MEDIA_IMAGES)
            if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                wanted.add(android.Manifest.permission.POST_NOTIFICATIONS)
        } else if (checkSelfPermission(android.Manifest.permission.READ_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            wanted.add(android.Manifest.permission.READ_EXTERNAL_STORAGE)
        }
        if (wanted.isEmpty()) launchService() else requestPermissions(wanted.toTypedArray(), 7)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == 7) launchService()   // proceed even if denied: SAF walker still works
    }

    private fun launchService() {
        SyncState.reset()
        SyncState.running = true
        startForegroundService(Intent(this, UploadService::class.java))
        toast("Started — progress updates below and in the notification")
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
