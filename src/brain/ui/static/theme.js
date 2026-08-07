/* Applies the stored theme before first paint.
 *
 * A separate, tiny, non-deferred file rather than an inline <script> — the CSP
 * forbids inline script, and this is the one thing that genuinely must run
 * before the first paint to avoid flashing the wrong ground colour.
 *
 * Dark is NOT the default. The OS decides unless the user has chosen. */
(function () {
  try {
    var stored = localStorage.getItem("brain-ui-theme");
    if (stored === "dark" || stored === "light") {
      document.documentElement.setAttribute("data-theme", stored);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  } catch (e) {
    /* localStorage can throw in private modes; the OS default is a fine
       fallback and a theme preference is never worth breaking boot over. */
  }
})();
