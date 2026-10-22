package expo.modules.docskeyboard

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.os.Build
import android.os.Bundle
import android.os.CancellationSignal
import android.os.SystemClock
import android.text.Editable
import android.text.InputFilter
import android.text.InputType
import android.text.Layout
import android.text.SpannableString
import android.text.Spanned
import android.text.StaticLayout
import android.text.TextWatcher
import android.text.style.LineHeightSpan
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.BaseInputConnection
import android.view.inputmethod.CompletionInfo
import android.view.inputmethod.CorrectionInfo
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.HandwritingGesture
import android.view.inputmethod.InputConnection
import android.view.inputmethod.InputConnectionWrapper
import android.view.inputmethod.InputContentInfo
import android.view.inputmethod.InputMethodManager
import android.view.inputmethod.PreviewableHandwritingGesture
import android.view.inputmethod.TextAttribute
import com.facebook.react.views.textinput.ContentSizeWatcher
import com.facebook.react.views.textinput.ReactEditText
import expo.modules.kotlin.AppContext
import expo.modules.kotlin.viewevent.EventDispatcher
import expo.modules.kotlin.views.ExpoView
import java.util.concurrent.Executor
import java.util.function.IntConsumer
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.roundToInt

/** One native text/input-connection identity for every logical outline row in an edit session. */
class DocsOutlineInputView(context: Context, appContext: AppContext) : ExpoView(context, appContext) {
  override val shouldUseAndroidLayout = true

  val onChange by EventDispatcher()
  val onSubmit by EventDispatcher()
  val onFocus by EventDispatcher()
  val onContentSizeChange by EventDispatcher()

  private var focusLifecycleReady = false
  private val state = OutlineInputState()
  private val editor = OutlineEditText(context)
  // The keyboard helper resolves the Expo wrapper's tag to this retained child.
  val input: ReactEditText get() = editor
  var diagnostics = false
    set(value) {
      field = value
      if (!value) awaitingFirstInput = null
    }
  private var awaitingFirstInput: AwaitingFirstInput? = null
  private var active = false
  private var replacingText = false
  private var pendingParagraphReset: PendingParagraphReset? = null
  private var pendingParagraphResetTimer: Runnable? = null
  private var lastHeight = Float.NaN
  private var lastHeightEpoch = -1L
  private var lastHeightGeneration = -1L
  private val fixedLineHeight = TypedValue.applyDimension(
    TypedValue.COMPLEX_UNIT_SP, 21f, resources.displayMetrics,
  ).roundToInt()
  private val lineHeightSpan = FixedLineHeightSpan(fixedLineHeight)
  private val reportSize = Runnable { reportContentSize() }

  private companion object {
    const val FROZEN_FRAME_TIMEOUT_MS = 120L
    const val MAX_FROZEN_TEXT_CHARS = 4096
    const val MAX_FROZEN_LAYOUT_LINES = 256
  }

  init {
    orientation = VERTICAL
    minimumHeight = dp(30f)
    editor.apply {
      layoutParams = LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.MATCH_PARENT)
      background = null
      setFontSize(15f)
      includeFontPadding = false
      gravity = Gravity.TOP or Gravity.START
      minimumHeight = dp(30f)
      minHeight = dp(30f)
      setPadding(dp(4f), dp(3f), dp(4f), dp(3f))
      setTextColor(Color.parseColor("#cdd6f4"))
      setHintTextColor(Color.parseColor("#6c7086"))
      highlightColor = Color.parseColor("#89b4fa")
      hint = SpannableString("メモ").apply {
        setSpan(FixedLineHeightSpan(fixedLineHeight), 0, length, Spanned.SPAN_INCLUSIVE_INCLUSIVE)
      }
      inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE or
        InputType.TYPE_TEXT_FLAG_CAP_SENTENCES
      filters = arrayOf(InputFilter.LengthFilter(20000))
      submitBehavior = "submit"
      returnKeyType = "done"
      setHorizontallyScrolling(false)
      isVerticalScrollBarEnabled = false
      isHorizontalScrollBarEnabled = false
      isEnabled = false
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
        textCursorDrawable?.mutate()?.let {
          it.setTint(Color.parseColor("#89b4fa"))
          textCursorDrawable = it
        }
      }
      onFocusChangeListener = View.OnFocusChangeListener { _, focused ->
        val snapshot = state.snapshot()
        if (focused) state.consumeFocus(snapshot.epoch)
        onFocus(mapOf("epoch" to snapshot.epoch, "generation" to snapshot.generation,
          "focused" to focused))
        trace(if (focused) 1 else 2)
      }
      setOnEditorActionListener { _, actionId, event ->
        if ((actionId and EditorInfo.IME_MASK_ACTION) != 0 || actionId == EditorInfo.IME_NULL) {
          handleEnter(event)
        } else true
      }
      addTextChangedListener(object : TextWatcher {
        override fun beforeTextChanged(text: CharSequence?, start: Int, count: Int, after: Int) = Unit
        override fun onTextChanged(text: CharSequence?, start: Int, before: Int, count: Int) = Unit
        override fun afterTextChanged(text: Editable?) {
          if (replacingText) return
          if (!text.isNullOrEmpty() && pendingParagraphReset != null) releasePendingParagraphFrame()
          applyLineHeight()
          emitChange(state.change(text?.toString() ?: ""))
          scheduleContentSize()
        }
      })
      setContentSizeWatcher(object : ContentSizeWatcher {
        override fun onLayout() { if (!replacingText) reportContentSize() }
      })
    }
    applyLineHeight()
    addView(editor)
    editor.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
      override fun onViewAttachedToWindow(view: View) { tryFulfillPendingFocus() }
      override fun onViewDetachedFromWindow(view: View) { state.cancelAllFocus() }
    })
    focusLifecycleReady = true
  }

  fun setControlledValue(value: Map<String, Any?>) {
    val epoch = counter(value["epoch"]) ?: return
    val generation = counter(value["generation"]) ?: return
    val eventCount = counter(value["eventCount"]) ?: return
    val text = value["text"] as? String ?: return
    val scope = if (value["scope"] == null) 0L else counter(value["scope"]) ?: return
    val restoreEventCount = if (value["restoreEventCount"] == null) 0L
      else counter(value["restoreEventCount"]) ?: return
    val rawReferences = (value["restoreFrom"] ?: emptyList<Any>()) as? List<*> ?: return
    val references = ArrayList<OutlineInputState.RestoreReference>()
    for (rawReference in rawReferences) {
      val reference = rawReference as? Map<*, *> ?: return
      val sourceEpoch = counter(reference["epoch"]) ?: return
      val sourceGeneration = counter(reference["generation"]) ?: return
      references.add(OutlineInputState.RestoreReference(sourceEpoch, sourceGeneration))
    }
    if (!state.canApplyControlled(epoch, generation, eventCount, text)) {
      trace(3)
      return
    }
    val previous = state.snapshot()
    val newEpoch = epoch > previous.epoch
    val resetInput = newEpoch || scope != state.getScope()
    // Drain the old connection's batches while its Editable still belongs to the old epoch.
    // Android closeConnection also finishes composition, so it must precede the new paragraph.
    if (resetInput) retireInputConnection()
    if (!state.applyControlled(scope, epoch, generation, eventCount, text,
      references.toTypedArray(), restoreEventCount)) {
      trace(3)
      return
    }
    if (resetInput || newEpoch || pendingParagraphReset?.let {
      it.epoch == epoch && it.generation == generation
    } == true) releasePendingParagraphFrame()
    if (resetInput) awaitingFirstInput = null
    val controlledText = state.snapshot().text
    val restoredFrom = state.restoredSnapshot()
    // A normal same-text echo must not touch selection or composing spans.
    if (resetInput || editor.text?.toString() != controlledText) {
      replacingText = true
      try {
        val editable = checkNotNull(editor.text)
        BaseInputConnection.removeComposingSpans(editable)
        if (editable.toString() != controlledText) {
          // The writer counts Unicode characters; Android's input filter counts UTF-16 units.
          // Existing accepted text must be displayed in full, even at the writer's limit.
          val inputFilters = editor.filters
          editor.filters = emptyArray()
          try {
            editable.replace(0, editable.length, controlledText)
          } finally {
            editor.filters = inputFilters
          }
        }
        applyLineHeight()
        editor.setSelection(editable.length)
      } finally {
        replacingText = false
      }
      if (resetInput && editor.isFocused) {
        // A manually selected paragraph must discard the previous IME candidate buffer.
        // Enter advances generation within the same epoch and never restarts its connection.
        val imm = editor.context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
        imm.restartInput(editor)
      }
      scheduleContentSize()
    }
    if (newEpoch && controlledText != text) {
      // Restore an undelivered native paragraph before the next key can mutate this epoch.
      // Send its full baseline with a fresh global event count so JS persists the recovery.
      emitChange(state.change(controlledText, restoredFrom))
    }
    tryFulfillPendingFocus()
  }

  fun setActive(value: Boolean) {
    active = value
    if (!value) {
      releasePendingParagraphFrame()
      state.cancelFocusThroughCurrentEpoch()
      awaitingFirstInput = null
      blurInput()
      retireInputConnection()
    }
    // Never hide the IME of a different focused input when an old overlay is disabled.
    editor.isEnabled = value
    tryFulfillPendingFocus()
  }

  fun focusInput(expectedEpoch: Long) {
    if (counter(expectedEpoch) == null) return
    val accepted = state.requestFocus(expectedEpoch)
    traceFocus(8, expectedEpoch, accepted)
    if (accepted) tryFulfillPendingFocus()
  }

  fun cancelFocus(expectedEpoch: Long) {
    if (counter(expectedEpoch) == null) return
    state.cancelFocus(expectedEpoch)
  }

  private fun tryFulfillPendingFocus() {
    if (!focusLifecycleReady) return
    val expectedEpoch = state.pendingFocusEpoch()
    if (expectedEpoch < 0) return
    val laidOut = editor.isLaidOut && editor.width > 0 && editor.height > 0
    if (!state.focusEligible(active, editor.isEnabled, editor.isAttachedToWindow,
      editor.isShown && isShown, editor.hasWindowFocus(), laidOut)) {
      traceFocus(8, expectedEpoch, false)
      return
    }
    if (!editor.isFocused) editor.requestFocusFromJS()
    // IMM may keep the same served/next view without rebuilding a connection on refocus.
    // Only a retired connection needs this restart; initial focus and Enter keep theirs.
    if (editor.isFocused && state.needsFreshConnection()) {
      val imm = editor.context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
      imm.restartInput(editor)
    }
    if (editor.isFocused) state.consumeFocus(expectedEpoch)
    traceFocus(9, expectedEpoch, editor.isFocused)
  }

  fun blurInput() {
    state.cancelFocusThroughCurrentEpoch()
    releasePendingParagraphFrame()
    if (!editor.isFocused) return
    val ownWindowToken = editor.windowToken
    retireInputConnection()
    editor.clearFocus()
    // This custom child is not registered with React Native TextInputState.
    // Clear its own IME explicitly, unless another text editor acquired focus meanwhile.
    if (ownWindowToken != null && rootView.findFocus()?.onCheckIsTextEditor() != true) {
      val imm = editor.context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
      imm.hideSoftInputFromWindow(ownWindowToken, 0)
    }
  }

  private fun retireInputConnection() {
    val previouslyReplacing = replacingText
    replacingText = true
    try {
      editor.retireConnection()
    } finally {
      replacingText = previouslyReplacing
    }
  }

  private fun handleEnter(event: KeyEvent?): Boolean {
    val enterStartedAt = if (diagnostics) SystemClock.uptimeMillis() else null
    if (!active || !editor.isEnabled || !editor.isFocused) return true
    if (event != null) {
      // TextView can deliver the same hardware Enter through its action and key paths.
      // Key-up never submits; separate key-downs (including repeats) remain separate actions.
      if (event.action != KeyEvent.ACTION_DOWN || !editor.claimEnter(event)) return true
    }
    // A second Enter can arrive before React sends the next controlled value.
    // Release the prior display freeze first; the Editable itself was already
    // cleared before the previous submit was dispatched.
    releasePendingParagraphFrame()
    val editable = checkNotNull(editor.text)
    if (hasComposition(editable)) {
      BaseInputConnection.removeComposingSpans(editable)
      emitChange(state.change(editable.toString()))
      trace(4)
      return true
    }

    val submission = state.advance(editable.toString())
    editor.captureFrozenFrame()
    replacingText = true
    try {
      // Keep the Editable and native InputConnection, not just the enclosing view.
      editable.replace(0, editable.length, "")
      applyLineHeight()
      editor.setSelection(0)
    } finally {
      replacingText = false
    }
    val source = submission.source
    // Keep a layout snapshot of the source paragraph over the cleared Editable until
    // React applies the next controlled generation. The actual input remains
    // empty, so the next IME event cannot append to the old paragraph.
    armPendingParagraphReset(source.epoch, submission.nextGeneration)
    onSubmit(mapOf("epoch" to source.epoch, "generation" to source.generation,
      "nextGeneration" to submission.nextGeneration, "eventCount" to source.eventCount,
      "text" to source.text, "focused" to editor.isFocused,
      "originEpoch" to source.originEpoch, "originGeneration" to source.originGeneration))
    awaitingFirstInput = enterStartedAt?.let {
      traceElapsed(6, state.snapshot(), SystemClock.uptimeMillis() - it)
      AwaitingFirstInput(source.epoch, submission.nextGeneration, it)
    }
    // Dispatch the source submit first, before any new-generation change.
    emitChange(state.change(""))
    scheduleContentSize()
    trace(5)
    return true
  }

  private fun hasComposition(text: Editable): Boolean =
    text.getSpans(0, text.length, Any::class.java).any {
      (text.getSpanFlags(it) and Spanned.SPAN_COMPOSING) != 0
    }

  private fun applyLineHeight() {
    editor.text?.let {
      it.setSpan(lineHeightSpan, 0, it.length, Spanned.SPAN_INCLUSIVE_INCLUSIVE)
    }
  }

  private fun emitChange(snapshot: OutlineInputState.Snapshot) {
    val payload = mapOf<String, Any>("epoch" to snapshot.epoch,
      "generation" to snapshot.generation, "eventCount" to snapshot.eventCount, "text" to snapshot.text,
      "originEpoch" to snapshot.originEpoch, "originGeneration" to snapshot.originGeneration)
    onChange(payload)
    val pending = awaitingFirstInput
    if (pending != null && snapshot.text.isNotEmpty() && snapshot.epoch == pending.epoch &&
      snapshot.generation == pending.generation) {
      awaitingFirstInput = null
      // Includes the user's inter-key wait: correlate with physical touch timing, not latency.
      traceElapsed(7, snapshot, SystemClock.uptimeMillis() - pending.enterUptimeMs)
    }
  }

  private fun scheduleContentSize() {
    removeCallbacks(reportSize)
    post(reportSize)
  }

  private fun reportContentSize() {
    if (!isAttachedToWindow || width == 0 || replacingText) return
    val layout = editor.layout ?: return
    val height = max(dp(30f), editor.compoundPaddingTop + layout.height +
      editor.compoundPaddingBottom) / resources.displayMetrics.density
    val snapshot = state.snapshot()
    if (height == lastHeight && snapshot.epoch == lastHeightEpoch &&
      snapshot.generation == lastHeightGeneration) return
    lastHeight = height
    lastHeightEpoch = snapshot.epoch
    lastHeightGeneration = snapshot.generation
    onContentSizeChange(mapOf("epoch" to snapshot.epoch, "generation" to snapshot.generation,
      "height" to height))
  }

  override fun onDetachedFromWindow() {
    state.cancelAllFocus()
    releasePendingParagraphFrame()
    removeCallbacks(reportSize)
    awaitingFirstInput = null
    blurInput()
    retireInputConnection()
    super.onDetachedFromWindow()
  }

  override fun onAttachedToWindow() {
    super.onAttachedToWindow()
    // A retained screen can reattach with unchanged bounds and no Fabric layout.
    // Expo's Android-layout pass restores the child's cleared isLaidOut flag.
    requestLayout()
    tryFulfillPendingFocus()
  }

  override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
    super.onLayout(changed, left, top, right, bottom)
    tryFulfillPendingFocus()
  }

  override fun onWindowFocusChanged(hasWindowFocus: Boolean) {
    super.onWindowFocusChanged(hasWindowFocus)
    if (hasWindowFocus) tryFulfillPendingFocus()
  }

  override fun onVisibilityChanged(changedView: View, visibility: Int) {
    super.onVisibilityChanged(changedView, visibility)
    if (!focusLifecycleReady) return
    if (!isShown) state.cancelAllFocus() else tryFulfillPendingFocus()
  }

  override fun onWindowVisibilityChanged(visibility: Int) {
    super.onWindowVisibilityChanged(visibility)
    if (!focusLifecycleReady) return
    if (visibility != View.VISIBLE) state.cancelAllFocus() else tryFulfillPendingFocus()
  }

  private fun trace(action: Int) {
    if (!diagnostics) return
    val snapshot = state.snapshot()
    Log.i("DocsOutlineInput", "action=$action epoch=${snapshot.epoch} " +
      "generation=${snapshot.generation} count=${snapshot.eventCount} " +
      "focused=${if (editor.isFocused) 1 else 0}")
  }

  private fun traceElapsed(action: Int, snapshot: OutlineInputState.Snapshot, elapsedMs: Long) {
    if (!diagnostics) return
    Log.i("DocsOutlineInput", "action=$action epoch=${snapshot.epoch} " +
      "generation=${snapshot.generation} count=${snapshot.eventCount} elapsedMs=$elapsedMs")
  }

  private fun traceFocus(action: Int, expectedEpoch: Long, result: Boolean) {
    if (!diagnostics) return
    val snapshot = state.snapshot()
    Log.i("DocsOutlineInput", "action=$action wantedEpoch=$expectedEpoch epoch=${snapshot.epoch} " +
      "active=${if (active) 1 else 0} enabled=${if (editor.isEnabled) 1 else 0} " +
      "attached=${if (editor.isAttachedToWindow) 1 else 0} shown=${if (editor.isShown && isShown) 1 else 0} " +
      "windowFocused=${if (editor.hasWindowFocus()) 1 else 0} focused=${if (editor.isFocused) 1 else 0} " +
      "laidOut=${if (editor.isLaidOut && editor.width > 0 && editor.height > 0) 1 else 0} " +
      "result=${if (result) 1 else 0}")
  }

  private data class AwaitingFirstInput(
    val epoch: Long,
    val generation: Long,
    val enterUptimeMs: Long,
  )

  private data class PendingParagraphReset(
    val epoch: Long,
    val generation: Long,
  )

  private fun armPendingParagraphReset(epoch: Long, generation: Long) {
    pendingParagraphResetTimer?.let(::removeCallbacks)
    pendingParagraphResetTimer = null
    pendingParagraphReset = null
    val pending = PendingParagraphReset(epoch, generation)
    pendingParagraphReset = pending
    val timer = Runnable {
      if (pendingParagraphReset === pending) releasePendingParagraphFrame()
    }
    pendingParagraphResetTimer = timer
    postDelayed(timer, FROZEN_FRAME_TIMEOUT_MS)
  }

  private fun releasePendingParagraphFrame() {
    pendingParagraphResetTimer?.let(::removeCallbacks)
    pendingParagraphResetTimer = null
    pendingParagraphReset = null
    editor.releaseFrozenFrame()
  }

  private fun dp(value: Float) = (value * resources.displayMetrics.density).roundToInt()

  private fun counter(value: Any?): Long? {
    val number = (value as? Number)?.toDouble() ?: return null
    if (!number.isFinite() || number < 0 || number > 9007199254740991.0 ||
      number != floor(number)) return null
    return number.toLong()
  }

  private inner class OutlineEditText(context: Context) : ReactEditText(context) {
    private var lastEnterDownTime = -1L
    private var lastEnterEventTime = -1L
    private var retainedConnection: OutlineInputConnection? = null
    private var frozenLayout: StaticLayout? = null

    fun captureFrozenFrame() {
      releaseFrozenFrame()
      val source = text?.let { SpannableString(it) } ?: return
      if (source.isEmpty()) return
      if (source.length > MAX_FROZEN_TEXT_CHARS) {
        trace(10)
        return
      }
      var sourceLineCount = 1
      for (index in 0 until source.length) {
        if (source[index] == '\n') sourceLineCount++
      }
      if (sourceLineCount > MAX_FROZEN_LAYOUT_LINES) {
        trace(11)
        return
      }
      val availableWidth = width - compoundPaddingLeft - compoundPaddingRight
      if (availableWidth <= 0) return
      val layout = StaticLayout.Builder.obtain(source, 0, source.length, paint, availableWidth)
        .setAlignment(Layout.Alignment.ALIGN_NORMAL)
        .setIncludePad(includeFontPadding)
        .build()
      if (layout.lineCount > MAX_FROZEN_LAYOUT_LINES) {
        trace(11)
        return
      }
      frozenLayout = layout
      invalidate()
    }

    fun releaseFrozenFrame() {
      frozenLayout = null
      invalidate()
    }

    override fun onDraw(canvas: Canvas) {
      val frozen = frozenLayout
      if (frozen == null) {
        super.onDraw(canvas)
        return
      }
      val save = canvas.save()
      canvas.clipRect(0, 0, width, height)
      canvas.translate(
        (compoundPaddingLeft - scrollX).toFloat(),
        (compoundPaddingTop - scrollY).toFloat(),
      )
      frozen.draw(canvas)
      canvas.restoreToCount(save)
    }

    fun retireConnection() {
      retainedConnection?.retire()
      retainedConnection = null
    }

    fun noteRetiredConnection(connection: OutlineInputConnection) {
      if (retainedConnection === connection) {
        retainedConnection = null
        state.connectionRetired()
      }
    }

    fun claimEnter(event: KeyEvent): Boolean {
      if (event.downTime == lastEnterDownTime && event.eventTime == lastEnterEventTime) return false
      lastEnterDownTime = event.downTime
      lastEnterEventTime = event.eventTime
      return true
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean =
      if (isEnterKey(keyCode)) handleEnter(event) else super.onKeyDown(keyCode, event)

    override fun onKeyUp(keyCode: Int, event: KeyEvent): Boolean =
      if (isEnterKey(keyCode)) true else super.onKeyUp(keyCode, event)

    override fun onCreateInputConnection(outAttrs: EditorInfo): InputConnection? {
      retireInputConnection()
      val connection = super.onCreateInputConnection(outAttrs) ?: return null
      return OutlineInputConnection(connection, state.snapshot().epoch).also {
        retainedConnection = it
        state.connectionCreated()
      }
    }

    private fun isEnterKey(keyCode: Int) =
      keyCode == KeyEvent.KEYCODE_ENTER || keyCode == KeyEvent.KEYCODE_NUMPAD_ENTER

  }

  /** Old IME callbacks must not mutate the same view after a manual paragraph switch. */
  private inner class OutlineInputConnection(connection: InputConnection, private val epoch: Long) :
    InputConnectionWrapper(connection, false) {
    private var retired = false

    private fun current() = !retired && active && editor.isAttachedToWindow && editor.isFocused &&
      state.acceptsEpoch(epoch)

    fun retire() {
      if (retired) return
      retired = true
      editor.noteRetiredConnection(this)
      // Only trusted view lifecycle code retires before replacing the old paragraph.
      // A late close from an old epoch must never finish the new epoch's composition.
      if (state.acceptsEpoch(epoch)) super.closeConnection()
    }

    override fun closeConnection() { if (current()) retire() }
    override fun beginBatchEdit(): Boolean = current() && super.beginBatchEdit()
    override fun endBatchEdit(): Boolean = current() && super.endBatchEdit()
    override fun clearMetaKeyStates(states: Int): Boolean = current() && super.clearMetaKeyStates(states)
    override fun finishComposingText(): Boolean = current() && super.finishComposingText()
    override fun setSelection(start: Int, end: Int): Boolean = current() && super.setSelection(start, end)
    override fun setComposingRegion(start: Int, end: Int): Boolean =
      current() && super.setComposingRegion(start, end)
    override fun setComposingText(text: CharSequence, newCursorPosition: Int): Boolean =
      current() && super.setComposingText(text, newCursorPosition)
    override fun deleteSurroundingText(beforeLength: Int, afterLength: Int): Boolean =
      current() && super.deleteSurroundingText(beforeLength, afterLength)
    override fun deleteSurroundingTextInCodePoints(beforeLength: Int, afterLength: Int): Boolean =
      current() && super.deleteSurroundingTextInCodePoints(beforeLength, afterLength)
    override fun commitCompletion(text: CompletionInfo): Boolean = current() && super.commitCompletion(text)
    override fun commitCorrection(correction: CorrectionInfo): Boolean = current() && super.commitCorrection(correction)
    override fun performContextMenuAction(id: Int): Boolean = current() && super.performContextMenuAction(id)
    override fun performPrivateCommand(action: String, data: Bundle?): Boolean =
      current() && super.performPrivateCommand(action, data)
    override fun reportFullscreenMode(enabled: Boolean): Boolean = current() && super.reportFullscreenMode(enabled)
    override fun requestCursorUpdates(cursorUpdateMode: Int): Boolean =
      current() && super.requestCursorUpdates(cursorUpdateMode)

    override fun performEditorAction(actionCode: Int): Boolean = current() && handleEnter(null)
    override fun sendKeyEvent(event: KeyEvent): Boolean = current() &&
      if (event.keyCode == KeyEvent.KEYCODE_ENTER || event.keyCode == KeyEvent.KEYCODE_NUMPAD_ENTER)
        handleEnter(event) else super.sendKeyEvent(event)
    override fun commitText(text: CharSequence, newCursorPosition: Int): Boolean = current() &&
      if (isEnterText(text)) handleEnter(null) else super.commitText(text, newCursorPosition)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.N_MR1)
    override fun commitContent(content: InputContentInfo, flags: Int, opts: Bundle?): Boolean =
      current() && super.commitContent(content, flags, opts)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.S)
    override fun performSpellCheck(): Boolean = current() && super.performSpellCheck()

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.S)
    override fun setImeConsumesInput(imeConsumesInput: Boolean): Boolean =
      current() && super.setImeConsumesInput(imeConsumesInput)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.TIRAMISU)
    override fun requestCursorUpdates(cursorUpdateMode: Int, cursorUpdateFilter: Int): Boolean =
      current() && super.requestCursorUpdates(cursorUpdateMode, cursorUpdateFilter)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.TIRAMISU)
    override fun setComposingRegion(start: Int, end: Int, textAttribute: TextAttribute?): Boolean =
      current() && super.setComposingRegion(start, end, textAttribute)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.TIRAMISU)
    override fun setComposingText(text: CharSequence, newCursorPosition: Int,
      textAttribute: TextAttribute?): Boolean =
      current() && super.setComposingText(text, newCursorPosition, textAttribute)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.TIRAMISU)
    override fun commitText(text: CharSequence, newCursorPosition: Int,
      textAttribute: TextAttribute?): Boolean = current() &&
      if (isEnterText(text)) handleEnter(null) else super.commitText(text, newCursorPosition, textAttribute)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
    override fun replaceText(start: Int, end: Int, text: CharSequence, newCursorPosition: Int,
      textAttribute: TextAttribute?): Boolean = current() &&
      if (isEnterText(text)) handleEnter(null)
      else super.replaceText(start, end, text, newCursorPosition, textAttribute)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
    override fun previewHandwritingGesture(gesture: PreviewableHandwritingGesture,
      cancellationSignal: CancellationSignal?): Boolean =
      current() && super.previewHandwritingGesture(gesture, cancellationSignal)

    @androidx.annotation.RequiresApi(Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
    override fun performHandwritingGesture(gesture: HandwritingGesture, executor: Executor?,
      consumer: IntConsumer?) {
      if (current()) {
        super.performHandwritingGesture(gesture, executor, consumer)
      } else if (consumer != null) {
        if (executor != null) executor.execute { consumer.accept(InputConnection.HANDWRITING_GESTURE_RESULT_CANCELLED) }
        else consumer.accept(InputConnection.HANDWRITING_GESTURE_RESULT_CANCELLED)
      }
    }

    private fun isEnterText(text: CharSequence) = text.toString() == "\n" ||
      text.toString() == "\r" || text.toString() == "\r\n"
  }

  private class FixedLineHeightSpan(private val height: Int) : LineHeightSpan {
    override fun chooseHeight(text: CharSequence, start: Int, end: Int, spanstartv: Int,
      lineHeight: Int, metrics: Paint.FontMetricsInt) {
      val leading = height - (metrics.descent - metrics.ascent)
      metrics.ascent -= ceil(leading / 2.0).toInt()
      metrics.descent += floor(leading / 2.0).toInt()
      if (start == 0) metrics.top = metrics.ascent
      if (end == text.length) metrics.bottom = metrics.descent
    }
  }
}
