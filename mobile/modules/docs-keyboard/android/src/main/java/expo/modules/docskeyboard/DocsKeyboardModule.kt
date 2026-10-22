package expo.modules.docskeyboard

import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.ViewTreeObserver
import android.view.WindowInsets
import android.view.inputmethod.InputMethodManager
import com.facebook.react.views.textinput.ReactEditText
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/** Native outline input and a cancellable IME-show helper for an already-focused Docs input. */
class DocsKeyboardModule : Module() {
  private val mainHandler = Handler(Looper.getMainLooper())
  private val nextToken = AtomicInteger(0)
  private val active = AtomicReference<PendingShow?>(null)
  private val foreground = AtomicBoolean(true)

  override fun definition() = ModuleDefinition {
    Name("DocsKeyboard")
    Constants("supportsOutlineInput" to true)

    View(DocsOutlineInputView::class) {
      Name("OutlineInput")
      Events("onChange", "onSubmit", "onFocus", "onContentSizeChange")
      Prop("value") { view: DocsOutlineInputView, value: Map<String, Any?> ->
        view.setControlledValue(value)
      }
      Prop("active") { view: DocsOutlineInputView, active: Boolean -> view.setActive(active) }
      Prop("diagnostics") { view: DocsOutlineInputView, diagnostics: Boolean ->
        view.diagnostics = diagnostics
      }
      // Expo view functions are dispatched on the main queue.
      AsyncFunction("focus") { view: DocsOutlineInputView, expectedEpoch: Long ->
        view.focusInput(expectedEpoch)
      }
      AsyncFunction("cancelFocus") { view: DocsOutlineInputView, expectedEpoch: Long ->
        view.cancelFocus(expectedEpoch)
      }
      AsyncFunction("blur") { view: DocsOutlineInputView -> view.blurInput() }
    }

    // Synchronous registration returns the token before JS can cancel it. Only
    // thread-safe state is touched here; all view/IMM work stays on the UI thread.
    Function("showForInput") { viewTag: Int, diagnostics: Boolean ->
      val request = PendingShow(nextToken.incrementAndGet(), viewTag, diagnostics)
      active.getAndSet(request)?.cancel()
      if (foreground.get()) mainHandler.post(request.start) else request.cancel()
      request.token
    }

    Function("cancel") { token: Int ->
      val request = active.get()
      if (request?.token == token && active.compareAndSet(request, null)) request.cancel()
    }

    OnActivityEntersForeground { foreground.set(true) }
    OnActivityEntersBackground { foreground.set(false); cancelActive() }
    OnActivityDestroys { foreground.set(false); cancelActive() }
    OnDestroy { foreground.set(false); cancelActive() }
  }

  private fun cancelActive() {
    active.getAndSet(null)?.cancel()
  }

  private inner class PendingShow(
    val token: Int,
    private val viewTag: Int,
    private val diagnostics: Boolean,
  ) : View.OnAttachStateChangeListener,
    ViewTreeObserver.OnWindowFocusChangeListener,
    ViewTreeObserver.OnGlobalFocusChangeListener {
    private val gate = KeyboardRequestGate()
    private var input: ReactEditText? = null
    private var observer: ViewTreeObserver? = null
    private var posted = false

    val start = Runnable {
      if (!current()) return@Runnable
      val view = resolveInput()
      // onFocus has already arrived. A removed/replaced/unfocused input must
      // never be recovered by focusing it again or by falling back to another.
      if (view == null || !view.hasFocus()) {
        finish()
        return@Runnable
      }
      input = view
      view.addOnAttachStateChangeListener(this)
      if (view.isAttachedToWindow) attached(view)
    }

    private val show = Runnable {
      posted = false
      val view = input
      if (view == null || !current() || resolveInput() !== view || !ready(view)
        || !gate.claimShow()) {
        finish()
        return@Runnable
      }
      try {
        val imm = view.context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
        // AndroidX uses this checkFocus side effect on API30–32 before Insets
        // show. It does not mean that the keyboard is visible.
        val served = imm.isActive(view)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
          val controller = view.windowInsetsController
          if (controller != null) controller.show(WindowInsets.Type.ime())
          else imm.showSoftInput(view, InputMethodManager.SHOW_IMPLICIT)
        } else {
          imm.showSoftInput(view, InputMethodManager.SHOW_IMPLICIT)
        }
        if (diagnostics || Log.isLoggable("DocsInputIme", Log.DEBUG)) {
          Log.i("DocsInputIme", "show_requested served=$served api=${Build.VERSION.SDK_INT}")
        }
      } finally {
        finish()
      }
    }

    private fun resolveInput(): ReactEditText? = try {
      when (val view = appContext.findView<View>(viewTag)) {
        is DocsOutlineInputView -> view.input
        is ReactEditText -> view
        else -> null
      }
    } catch (_: Exception) {
      null
    }

    private fun current() = gate.isPending && active.get() === this

    private fun ready(view: ReactEditText) = view.isAttachedToWindow && view.hasWindowFocus()
      && view.isFocused && view.rootView.findFocus() === view && view.isShown
      && view.isEnabled && view.showSoftInputOnFocus

    private fun attached(view: ReactEditText) {
      if (!current() || !view.hasFocus()) {
        finish()
        return
      }
      // A detached view's observer can be replaced on attach. Hold exactly the
      // attached observer so every installed listener can be removed later.
      observer = view.viewTreeObserver.also {
        it.addOnWindowFocusChangeListener(this)
        it.addOnGlobalFocusChangeListener(this)
      }
      scheduleShow(view)
    }

    private fun scheduleShow(view: ReactEditText) {
      if (!current() || !view.hasFocus()) {
        finish()
        return
      }
      if (!view.hasWindowFocus() || posted) return
      posted = true
      // Let the native focus/window event finish before asking IME to show.
      // Unlike a JS timer or Fabric onLayout, this runs on the view's UI queue.
      if (!view.post(show)) finish()
    }

    override fun onViewAttachedToWindow(view: View) {
      (view as? ReactEditText)?.let { attached(it) }
    }

    override fun onViewDetachedFromWindow(view: View) = finish()

    override fun onWindowFocusChanged(hasFocus: Boolean) {
      if (!hasFocus) finish() else input?.let { scheduleShow(it) }
    }

    override fun onGlobalFocusChanged(oldFocus: View?, newFocus: View?) {
      if (newFocus !== input) finish()
    }

    fun cancel() {
      gate.cancel() // Immediate, even when JS cancels before native start/post.
      mainHandler.post { finish() }
    }

    private fun finish() {
      gate.cancel()
      mainHandler.removeCallbacks(start)
      input?.removeCallbacks(show)
      input?.removeOnAttachStateChangeListener(this)
      observer?.takeIf { it.isAlive }?.let {
        it.removeOnWindowFocusChangeListener(this)
        it.removeOnGlobalFocusChangeListener(this)
      }
      observer = null
      input = null
      posted = false
      active.compareAndSet(this, null)
    }
  }
}
