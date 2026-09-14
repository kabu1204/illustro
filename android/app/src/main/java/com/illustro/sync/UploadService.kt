package com.illustro.sync

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.IBinder
import android.os.PowerManager
import android.provider.DocumentsContract
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import java.time.Duration
import java.util.Locale
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

class UploadService : Service() {

    companion object {
        const val ACTION_STOP = "com.illustro.sync.STOP"
        val stopFlag = AtomicBoolean()

        private val CLIENT: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(Duration.ofSeconds(15))
            .writeTimeout(Duration.ofSeconds(300))
            .readTimeout(Duration.ofSeconds(60))
            .build()

        private val OCTET = "application/octet-stream".toMediaType()
        private val IMAGE_EXTS = setOf("jpg", "jpeg", "png", "webp", "bmp", "gif")
        // LinkedBlockingQueue forbids null elements; use a sentinel to end consumers.
        private val POISON = Job(Source("", 0, 0, null, null), "", "")
        private fun isImage(name: String): Boolean {
            val dot = name.lastIndexOf('.')
            if (dot < 0 || dot == name.length - 1) return false
            return name.substring(dot + 1).lowercase(Locale.ROOT) in IMAGE_EXTS
        }
    }

    private class Source(
        val name: String, val size: Long, val mtime: Long,
        val file: File?, val uri: Uri?
    )

    private class Job(val src: Source, val sha: String, val mk: String)

    private lateinit var db: MetaDb
    private val uploaded = AtomicInteger()
    private val known = AtomicInteger()
    private val failed = AtomicInteger()
    private val processed = AtomicInteger()

    private lateinit var wakeLock: PowerManager.WakeLock

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        channel()
        if (intent?.action == ACTION_STOP) {
            stopFlag.set(true)
            notifyBar("illustro sync", "Stopping — waiting for the current file…")
            return START_NOT_STICKY
        }
        val n = bar("starting", "")
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(1, n)
        }
        Thread { engine() }.start()
        return START_NOT_STICKY
    }

    // ---------------------------------------------------------------- engine

    private fun engine() {
        stopFlag.set(false)
        SyncState.reset()
        SyncState.running = true
        db = MetaDb(this)

        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "illustro:sync")
            .apply { setReferenceCounted(false); acquire(6 * 3600 * 1000L) }

        val base = Prefs.get(this, "url").trimEnd('/')
        val token = Prefs.get(this, "token")
        SyncState.folder = Prefs.get(this, "folderLabel")

        val queue = LinkedBlockingQueue<Job>()
        try {
            // 1) connectivity / config probe
            SyncState.phase = "connecting"
            val info = try {
                CLIENT.newCall(Request.Builder().url("$base/api/sync/info").build()).execute().use { r ->
                    val text = r.body?.string() ?: ""
                    if (r.code == 401) throw IOException("401: token required but wrong/missing")
                    if (!r.isSuccessful) throw IOException("server returned HTTP ${r.code}")
                    JSONObject(text)
                }
            } catch (e: Exception) {
                throw IOException("cannot reach $base — ${e.message}")
            }
            if (!info.optBoolean("enabled", false)) throw IOException("sync is disabled on the server")

            // 2) enumerate folder (live discovered-count)
            SyncState.phase = "scanning"
            val treeUri = Uri.parse(Prefs.get(this, "treeUri"))
            val sources = enumerate(treeUri)
            if (sources.isEmpty()) throw IOException("no images found in the picked folder")
            SyncState.total = sources.size

            // 3) hash (producer) + upload (2 consumers); progress reported per file
            SyncState.phase = "uploading"
            val consumers = listOf(
                Thread { consume(base, token, queue) },
                Thread { consume(base, token, queue) }
            )
            consumers.forEach { it.start() }

            for (src in sources) {
                if (stopFlag.get()) break
                SyncState.current = src.name
                val mk = "${src.name}|${src.size}|${src.mtime}"
                if (db.isDone(mk)) {
                    known.incrementAndGet(); processed.incrementAndGet()
                    SyncState.known = known.get(); SyncState.processed = processed.get()
                    continue
                }
                try {
                    val sha = db.hashFor(mk) ?: hashSource(src).also { db.put(mk, it, false) }
                    queue.put(Job(src, sha, mk))
                } catch (e: Exception) {
                    failed.incrementAndGet(); processed.incrementAndGet()
                    SyncState.failed = failed.get(); SyncState.processed = processed.get()
                    SyncState.lastError = "hash failed: ${src.name} (${e.message})"
                }
            }
            queue.put(POISON)   // sentinel: one per consumer
            queue.put(POISON)
            consumers.forEach { it.join() }
        } catch (e: Exception) {
            SyncState.lastError = e.message ?: e.toString()
        } finally {
            SyncState.running = false
            SyncState.finished = true
            SyncState.phase = "done"
            if (wakeLock.isHeld) wakeLock.release()
            finalNotify()
            stopSelf()
        }
    }

    // ------------------------------------------------------------- enumeration

    /** Direct java.io walk on primary storage (fast path); SAF query walk otherwise. */
    private fun enumerate(treeUri: Uri): MutableList<Source> {
        val out = mutableListOf<Source>()
        val direct = directDir(treeUri)
        if (direct != null && direct.isDirectory) {
            walkFile(direct, out)
            if (out.isNotEmpty()) return out
        }
        walkSaf(treeUri, DocumentsContract.getTreeDocumentId(treeUri), out, 0)
        return out
    }

    private fun directDir(treeUri: Uri): File? = try {
        val docId = DocumentsContract.getTreeDocumentId(treeUri)   // "primary:Pictures/anime"
        val volume = docId.substringBefore(':', "")
        val path = docId.substringAfter(':', "")
        if (volume == "primary" && path.isNotEmpty())
            File(Environment.getExternalStorageDirectory(), path) else null
    } catch (_: Exception) {
        null
    }

    private fun walkFile(dir: File, out: MutableList<Source>) {
        val children = dir.listFiles() ?: return
        for (f in children) {
            if (f.isDirectory) walkFile(f, out)
            else if (f.isFile && isImage(f.name)) {
                out.add(Source(f.name, f.length(), f.lastModified(), f, null))
                SyncState.total = out.size
                SyncState.current = f.name
            }
        }
    }

    private fun walkSaf(treeUri: Uri, parentDocId: String, out: MutableList<Source>, depth: Int) {
        if (depth > 15) return
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocId)
        val proj = arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
            DocumentsContract.Document.COLUMN_SIZE,
            DocumentsContract.Document.COLUMN_LAST_MODIFIED
        )
        try {
            contentResolver.query(childrenUri, proj, null, null, null)?.use { c ->
                val iId = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
                val iName = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
                val iMime = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_MIME_TYPE)
                val iSize = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_SIZE)
                val iMtime = c.getColumnIndexOrThrow(DocumentsContract.Document.COLUMN_LAST_MODIFIED)
                while (c.moveToNext()) {
                    if (stopFlag.get()) return
                    val docId = c.getString(iId)
                    val name = c.getString(iName) ?: continue
                    val mime = c.getString(iMime) ?: ""
                    if (mime == DocumentsContract.Document.MIME_TYPE_DIR) {
                        walkSaf(treeUri, docId, out, depth + 1)
                    } else if (isImage(name)) {
                        out.add(
                            Source(
                                name, c.getLong(iSize), c.getLong(iMtime), null,
                                DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)
                            )
                        )
                        SyncState.total = out.size
                        SyncState.current = name
                    }
                }
            }
        } catch (_: Exception) {
            // unreadable subtree: skip it
        }
    }

    // -------------------------------------------------------------- processing

    private fun hashSource(src: Source): String {
        val md = MessageDigest.getInstance("SHA-256")
        val buf = ByteArray(1 shl 20)
        val input: InputStream = if (src.file != null) FileInputStream(src.file)
        else contentResolver.openInputStream(src.uri!!) ?: throw IOException("cannot open ${src.name}")
        input.use { s ->
            while (true) {
                val n = s.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { String.format(Locale.ROOT, "%02x", it) }
    }

    /** SAF sources are copied to a cache file first (hash + multipart both need a File/InputStream). */
    private fun materialize(src: Source): File = if (src.file != null) src.file
    else {
        val safe = src.name.replace(Regex("[/\\\\]"), "_").takeLast(48).ifEmpty { "upload" }
        val tmp = File(cacheDir, "up_${System.nanoTime()}_$safe")
        contentResolver.openInputStream(src.uri!!)!!.use { input ->
            FileOutputStream(tmp).use { input.copyTo(it, 1 shl 20) }
        }
        tmp
    }

    private fun consume(base: String, token: String, queue: LinkedBlockingQueue<Job>) {
        while (true) {
            val job: Job
            try { job = queue.take() } catch (e: InterruptedException) { return }
            if (job === POISON || stopFlag.get()) return
            var tmp: File? = null
            try {
                tmp = materialize(job.src)
                var attempt = 0
                while (true) {
                    try {
                        val result = uploadOne(base, token, job.src.name, tmp, job.sha)
                        when (result) {
                            "stored" -> uploaded.incrementAndGet()
                            "duplicate" -> known.incrementAndGet()
                            else -> {
                                failed.incrementAndGet()
                                SyncState.lastError = "$result: ${job.src.name}"
                            }
                        }
                        db.put(job.mk, job.sha, done = result != "toolarge" && result != "unsupported")
                        processed.incrementAndGet()
                        if (result == "stored") SyncState.bytesSent.addAndGet(tmp.length())
                        break
                    } catch (e: Exception) {
                        attempt++
                        if (attempt >= 3 || stopFlag.get()) {
                            failed.incrementAndGet()
                            processed.incrementAndGet()
                            SyncState.lastError = "upload failed: ${job.src.name} (${e.message})"
                            break
                        }
                        Thread.sleep(1500L * attempt)
                    }
                }
            } catch (e: Exception) {
                failed.incrementAndGet()
                processed.incrementAndGet()
                SyncState.lastError = "skipped ${job.src.name}: ${e.message}"
            } finally {
                tmp?.let { if (it != job.src.file) it.delete() }
                pushState(job.src.name)
            }
        }
    }

    /** Returns "stored" | "duplicate" | "toolarge" | "unsupported"; throws on transient errors. */
    private fun uploadOne(base: String, token: String, name: String, file: File, sha: String): String {
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("sha256", sha)
            .addFormDataPart("file", name, file.asRequestBody(OCTET))
            .build()
        val rb = Request.Builder().url("$base/api/sync/upload")
        if (token.isNotEmpty()) rb.header("X-API-Token", token)
        CLIENT.newCall(rb.post(body).build()).execute().use { r ->
            val text = r.body?.string() ?: ""
            when {
                r.code == 200 ->
                    return if (JSONObject(text).optString("status") == "stored") "stored" else "duplicate"
                r.code == 401 -> throw IOException("401: wrong or missing token")
                r.code == 413 -> return "toolarge"
                r.code == 415 -> return "unsupported"
                else -> throw IOException("HTTP ${r.code}")
            }
        }
    }

    // ------------------------------------------------------------- progress UI

    private fun pushState(current: String) {
        SyncState.processed = processed.get()
        SyncState.uploaded = uploaded.get()
        SyncState.known = known.get()
        SyncState.failed = failed.get()
        SyncState.current = current
        notifyBar("illustro sync — ${SyncState.phase}", brief())
    }

    private fun brief(): String =
        "${SyncState.processed}/${SyncState.total} · new ${SyncState.uploaded} · " +
            "known ${SyncState.known} · failed ${SyncState.failed}"

    private fun pi(): PendingIntent = PendingIntent.getActivity(
        this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
    )

    private fun channel() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(NotificationChannel("sync", "Upload progress", NotificationManager.IMPORTANCE_LOW))
    }

    private fun bar(title: String, text: String): Notification =
        Notification.Builder(this, "sync")
            .setContentTitle(title)
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pi())
            .build()

    private fun notifyBar(title: String, text: String) {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.notify(1, bar(title, text))
    }

    private fun finalNotify() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val title = if (stopFlag.get()) "Stopped" else "Finished"
        val n = Notification.Builder(this, "sync")
            .setContentTitle("illustro sync — $title")
            .setContentText("${SyncState.uploaded} new · ${SyncState.known} already known · ${SyncState.failed} failed")
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setAutoCancel(true)
            .setContentIntent(pi())
            .build()
        stopForeground(Service.STOP_FOREGROUND_DETACH)
        nm.notify(1, n)
    }
}
