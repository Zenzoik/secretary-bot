(function (root) {
  "use strict";
  const api = {
    localDateTime(value) {
      if (!value) return "";
      const point = new Date(value);
      const pad = number => String(number).padStart(2, "0");
      return `${point.getFullYear()}-${pad(point.getMonth() + 1)}-${pad(point.getDate())}T${pad(point.getHours())}:${pad(point.getMinutes())}`;
    },
    utcDateTime(value) { return value ? new Date(value).toISOString() : null; },
    // A datetime-local field cannot express the UTC offset, so during the repeated
    // autumn hour two instants share one field value. Keep the stored instant while
    // the field is untouched; only a value the user actually changed is re-parsed,
    // and an ambiguous new value resolves to its first (summer-time) occurrence.
    resolveDateTime(fieldValue, originalIso) {
      if (!fieldValue) return null;
      if (originalIso && api.localDateTime(originalIso) === fieldValue) return new Date(originalIso).toISOString();
      return api.utcDateTime(fieldValue);
    },
    botMaximum(value) { return Math.min(Number(value), 60); },
  };
  root.SecretaryUI = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window === "undefined" ? globalThis : window);
