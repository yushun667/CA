/* Function detail view — accessible splitter handles.
 *
 * Three separators resize the workspace: the top source area (horizontal
 * separator, vertical drag), the left accordion sidebar and the right
 * node-source column (vertical separators, horizontal drag; the right column
 * is inverted because its width grows when the handle moves left).
 *
 * Each handle is `role="separator"` with `aria-orientation` / `aria-valuenow`,
 * supports pointer drag, double-click reset and arrow-key nudging.
 */

const STEP = 16;

export function createSplitter(React) {
  const { useEffect, useRef } = React;

  return function Splitter(props) {
    const cleanups = useRef([]);

    function removeDragListeners() {
      const list = cleanups.current;
      cleanups.current = [];
      for (const remove of list) {
        try {
          remove();
        } catch (_error) {
          /* listener target already gone */
        }
      }
    }

    useEffect(() => {
      return function cleanup() {
        removeDragListeners();
      };
    }, []);

    const disabled = Boolean(props.disabled);
    const orientation = props.orientation === "vertical" ? "vertical" : "horizontal";
    const invert = Boolean(props.invert);
    const min = Number.isFinite(Number(props.min)) ? Number(props.min) : 0;
    const max = Number.isFinite(Number(props.max)) ? Number(props.max) : 0;
    const current = Number.isFinite(Number(props.value)) ? Number(props.value) : 0;

    function clampValue(value) {
      const boundedMax = max > min ? max : Math.max(min, value);
      return Math.round(Math.min(Math.max(value, min), boundedMax));
    }

    function measuredValue() {
      if (typeof props.measure === "function") {
        try {
          const size = Number(props.measure());
          if (Number.isFinite(size) && size > 0) return size;
        } catch (_error) {
          /* fall back to the controlled value */
        }
      }
      return current;
    }

    function emit(next) {
      if (typeof props.onResize === "function") props.onResize(clampValue(next));
    }

    function onPointerDown(event) {
      if (disabled) return;
      if (event.button !== undefined && event.button !== 0) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      const startValue = measuredValue();
      const startX = event.clientX;
      const startY = event.clientY;
      const globalScope = typeof window !== "undefined" ? window : null;
      if (!globalScope) return;

      function onMove(moveEvent) {
        const delta =
          orientation === "vertical"
            ? moveEvent.clientX - startX
            : moveEvent.clientY - startY;
        emit(startValue + (invert ? -delta : delta));
      }

      function onUp() {
        removeDragListeners();
      }

      globalScope.addEventListener("pointermove", onMove);
      globalScope.addEventListener("pointerup", onUp);
      globalScope.addEventListener("pointercancel", onUp);
      cleanups.current = [
        function () {
          globalScope.removeEventListener("pointermove", onMove);
        },
        function () {
          globalScope.removeEventListener("pointerup", onUp);
          globalScope.removeEventListener("pointercancel", onUp);
        },
      ];
    }

    function onDoubleClick(event) {
      if (disabled) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      if (typeof props.onReset === "function") props.onReset();
    }

    function onKeyDown(event) {
      if (disabled) return;
      let delta = 0;
      if (orientation === "horizontal") {
        if (event.key === "ArrowUp") delta = -STEP;
        else if (event.key === "ArrowDown") delta = STEP;
      } else {
        if (event.key === "ArrowLeft") delta = invert ? STEP : -STEP;
        else if (event.key === "ArrowRight") delta = invert ? -STEP : STEP;
      }
      if (!delta) return;
      if (typeof event.preventDefault === "function") event.preventDefault();
      emit(measuredValue() + delta);
    }

    const className =
      "ca-fn-splitter ca-fn-splitter-" +
      orientation +
      (invert ? " is-inverted" : "") +
      (disabled ? " is-disabled" : "");

    return React.createElement(
      "div",
      {
        className,
        role: "separator",
        tabIndex: disabled ? -1 : 0,
        "aria-orientation": orientation,
        "aria-valuenow": Math.round(current),
        "aria-valuemin": Math.round(min),
        "aria-valuemax": Math.round(max),
        "aria-label": props.label || undefined,
        title: props.label || undefined,
        "data-splitter": props.kind || orientation,
        onPointerDown,
        onDoubleClick,
        onKeyDown,
      },
      React.createElement("span", {
        className: "ca-fn-splitter-grip",
        "aria-hidden": "true",
      })
    );
  };
}
