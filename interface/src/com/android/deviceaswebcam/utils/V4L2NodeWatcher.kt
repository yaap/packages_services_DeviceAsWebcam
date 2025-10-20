/*
 * Copyright (C) 2025 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.android.deviceaswebcam.utils

import android.os.FileObserver
import android.os.HandlerThread
import android.util.Log
import java.io.File

class V4L2NodeWatcher() {

    private val callbackThread by lazy { HandlerThread("V4L2NodeWatcher").also { it.start() } }
    private val handler by lazy { callbackThread.threadHandler }

    private var fileObserver: FileObserver? = null

    /**
     * Starts the watcher. The callback will be invoked on the [callbackThread].
     *
     * @param callback The callback to be invoked when a new V4L2 node is detected.
     */
    @Synchronized
    fun start(callback: Runnable) {
        maybeLogV(VERBOSE, TAG) { "Starting V4L2NodeWatcher" }

        if (fileObserver != null) {
            maybeLogV(VERBOSE, TAG) { "FileObserver already running. No-op-ing." }
            return
        }

        // Immediately report the initial state to the caller.
        handler.post(callback)

        fileObserver =
            object : FileObserver(File(V4L2_NODE_DIR), FileObserver.CREATE or FileObserver.DELETE) {
                    override fun onEvent(event: Int, path: String?) {
                        maybeLogV(VERBOSE, TAG) { "onEvent: $event, $path" }
                        // Offload the work to the handler to prevent the event thread from being
                        // blocked for any amount of time.
                        handler.post {
                            if (path?.startsWith(V4L2_NODE_PREFIX) ?: false) {
                                callback.run()
                            }
                        }
                    }
                }
                .also { it.startWatching() }
        maybeLogV(VERBOSE, TAG) { "FileObserver started" }
        handler.postDelayed({ stop() }, WATCH_TIMEOUT_MS)
    }

    @Synchronized
    fun stop() {
        maybeLogV(VERBOSE, TAG) { "Stopping V4L2NodeWatcher" }
        fileObserver?.stopWatching() ?: return // return early if the watcher is already stopped.
        handler.removeCallbacksAndMessages(null)
        fileObserver = null
    }

    companion object {
        private val TAG = V4L2NodeWatcher::class.java.simpleName
        private val VERBOSE = Log.isLoggable(TAG, Log.VERBOSE)

        private const val V4L2_NODE_DIR = "/dev"
        private const val V4L2_NODE_PREFIX = "video"
        private const val WATCH_TIMEOUT_MS = 5_000L // 5 seconds
    }
}

private fun maybeLogV(verbose: Boolean, tag: String, msg: () -> String) {
    if (verbose) {
        Log.v(tag, msg())
    }
}
