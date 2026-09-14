package com.illustro.sync

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.util.Locale
import java.util.concurrent.atomic.AtomicLong

/** Live progress, polled by the activity UI and mirrored into the notification. */
object SyncState {
    @Volatile var running = false
    @Volatile var finished = false
    @Volatile var phase = ""          // connecting / scanning / processing / done
    @Volatile var folder = ""
    @Volatile var total = 0           // images discovered (grows live during scan)
    @Volatile var processed = 0       // finished one way or another
    @Volatile var uploaded = 0        // newly stored on server this run
    @Volatile var known = 0           // already on server / previously uploaded
    @Volatile var failed = 0
    @Volatile var current = ""        // file being worked on right now
    @Volatile var lastError = ""
    val bytesSent = AtomicLong()
    val startMs = AtomicLong()

    fun reset() {
        finished = false
        phase = ""
        total = 0
        processed = 0
        uploaded = 0
        known = 0
        failed = 0
        current = ""
        lastError = ""
        bytesSent.set(0)
        startMs.set(System.currentTimeMillis())
    }

    private fun fmtBytes(b: Long): String = when {
        b >= 1L shl 30 -> String.format(Locale.ROOT, "%.2f GB", b.toDouble() / (1L shl 30))
        b >= 1L shl 20 -> String.format(Locale.ROOT, "%.1f MB", b.toDouble() / (1L shl 20))
        b >= 1L shl 10 -> String.format(Locale.ROOT, "%.0f KB", b.toDouble() / (1L shl 10))
        else -> "$b B"
    }

    fun statusText(): String = buildString {
        if (folder.isNotEmpty()) append(folder).append('\n')
        if (phase.isNotEmpty()) append('[').append(phase).append("]  ")
        append(processed).append('/').append(total)
            .append(" done · new ").append(uploaded)
            .append(" · known ").append(known)
            .append(" · failed ").append(failed).append('\n')
        val secs = ((System.currentTimeMillis() - startMs.get()) / 1000).coerceAtLeast(1)
        val b = bytesSent.get()
        if (b > 0) {
            append(fmtBytes(b)).append(" sent · ")
            append(fmtBytes(b / secs)).append("/s").append('\n')
        }
        if (running) append("▸ ").append(current)
        else if (finished) append("finished — tap Start to rescan for new files")
        if (lastError.isNotEmpty()) append("\nerror: ").append(lastError)
    }
}

object Prefs {
    private const val FILE = "prefs"
    fun get(ctx: Context, k: String): String =
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(k, "") ?: ""

    fun put(ctx: Context, k: String, v: String) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().putString(k, v).apply()
    }
}

/** Local ledger: (name|size|mtime) -> sha256 + uploaded flag. Survives restarts, so a
 *  re-run of the same folder skips finished files instantly without re-hashing. */
class MetaDb(ctx: Context) : SQLiteOpenHelper(ctx, "sync.db", null, 1) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE IF NOT EXISTS meta (mk TEXT PRIMARY KEY, hash TEXT, done INTEGER DEFAULT 0)")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {}

    @Synchronized
    fun isDone(mk: String): Boolean =
        readableDatabase.rawQuery("SELECT done FROM meta WHERE mk=?", arrayOf(mk)).use {
            it.moveToFirst() && it.getInt(0) == 1
        }

    @Synchronized
    fun hashFor(mk: String): String? =
        readableDatabase.rawQuery("SELECT hash FROM meta WHERE mk=?", arrayOf(mk)).use {
            if (it.moveToFirst()) it.getString(0) else null
        }

    @Synchronized
    fun put(mk: String, hash: String, done: Boolean) {
        writableDatabase.execSQL(
            "INSERT OR REPLACE INTO meta (mk, hash, done) VALUES (?,?,?)",
            arrayOf(mk, hash, if (done) 1 else 0)
        )
    }
}
