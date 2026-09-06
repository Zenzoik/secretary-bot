(function (root) {
  "use strict";
  const api = {
    // The bot applies pauses in its own timezone, so the field shows that wall
    // clock rather than the one the owner's device happens to be set to.
    zoneParts(instant, timeZone) {
      const formatter = new Intl.DateTimeFormat("en-CA", {
        timeZone, hour12: false, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
      const parts = {};
      for (const part of formatter.formatToParts(instant)) {
        if (part.type !== "literal") parts[part.type] = part.value;
      }
      return parts;
    },
    zoneOffset(instant, timeZone) {
      const parts = api.zoneParts(instant, timeZone);
      const wall = Date.UTC(
        Number(parts.year), Number(parts.month) - 1, Number(parts.day),
        Number(parts.hour) % 24, Number(parts.minute), Number(parts.second),
      );
      return wall - instant.getTime();
    },
    zonedDateTime(value, timeZone) {
      if (!value) return "";
      const parts = api.zoneParts(new Date(value), timeZone);
      const hour = String(Number(parts.hour) % 24).padStart(2, "0");
      return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`;
    },
    utcDateTime(value, timeZone) {
      if (!value) return null;
      const wall = Date.parse(`${value}:00Z`);
      if (Number.isNaN(wall)) return null;
      // The offset depends on the instant, which is what is being solved for, so
      // the first guess is corrected once — enough for a one-hour DST step.
      let instant = new Date(wall - api.zoneOffset(new Date(wall), timeZone));
      const offset = api.zoneOffset(instant, timeZone);
      if (wall - offset !== instant.getTime()) instant = new Date(wall - offset);
      return instant.toISOString();
    },
    // A datetime-local field cannot express the UTC offset, so during the repeated
    // autumn hour two instants share one field value. Keep the stored instant while
    // the field is untouched; only a value the user actually changed is re-parsed,
    // and an ambiguous new value resolves to its first (summer-time) occurrence.
    resolveDateTime(fieldValue, originalIso, timeZone) {
      if (!fieldValue) return null;
      if (originalIso && api.zonedDateTime(originalIso, timeZone) === fieldValue) {
        return new Date(originalIso).toISOString();
      }
      return api.utcDateTime(fieldValue, timeZone);
    },
    botMaximum(value) { return Math.min(Number(value), 60); },
  };
  root.SecretaryUI = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window === "undefined" ? globalThis : window);
