"""
App Launcher

Deterministic application / URL launching that bypasses the vision model.

Why this exists:
    Opening an app by asking the model to *find and click* its taskbar icon is
    the single most fragile step in the loop — the icon may not be pinned, its
    position is unknown, and a misclick gives no feedback, so the model spins in
    an oscillation loop guessing coordinates. Launching deterministically removes
    that entire failure mode: we shell out to the OS and let Windows place the
    window, then the model only has to operate the app once it is visibly open.

Design notes:
    - Windows-first. Browsers are launched via the `start` shell builtin (so the
      user's default-profile / already-running instance is reused and a URL can
      be handed straight to it). Generic apps fall back to os.startfile / the
      executable name on PATH.
    - A small alias table maps friendly names ("chrome", "vscode") to the
      concrete launch strategy, so the model can emit [LAUNCH:chrome] without
      knowing the executable path.
    - Pure side-effect methods raise on failure; the Coordinator translates that
      into an "error" result so the existing recovery path engages.
"""

import logging
import shutil
import subprocess
import webbrowser

logger = logging.getLogger(__name__)


# Friendly name → launch spec. "browser" entries are opened so a URL can later
# be routed to them; "exe" entries are plain executables resolved on PATH.
# Names are matched case-insensitively after stripping surrounding whitespace.
_APP_ALIASES: dict[str, dict[str, str]] = {
    "chrome":        {"kind": "browser", "exe": "chrome"},
    "google chrome": {"kind": "browser", "exe": "chrome"},
    "edge":          {"kind": "browser", "exe": "msedge"},
    "msedge":        {"kind": "browser", "exe": "msedge"},
    "firefox":       {"kind": "browser", "exe": "firefox"},
    "notepad":       {"kind": "exe",     "exe": "notepad"},
    "explorer":      {"kind": "exe",     "exe": "explorer"},
    "vscode":        {"kind": "exe",     "exe": "code"},
    "code":          {"kind": "exe",     "exe": "code"},
    "calc":          {"kind": "exe",     "exe": "calc"},
    "calculator":    {"kind": "exe",     "exe": "calc"},
    "cmd":           {"kind": "exe",     "exe": "cmd"},
    "powershell":    {"kind": "exe",     "exe": "powershell"},
    "terminal":      {"kind": "exe",     "exe": "wt"},
}


class AppLauncher:
    """Launch applications and URLs deterministically, without the vision model."""

    def launch_app(self, name: str) -> None:
        """
        Launch the application identified by *name*.

        Resolves *name* through the alias table first; if unknown, falls back to
        treating *name* itself as an executable on PATH. Raises RuntimeError if
        nothing launchable can be found.
        """
        key = name.strip().lower()
        spec = _APP_ALIASES.get(key)

        if spec is not None:
            exe = spec["exe"]
            logger.info("launch_app: %r → alias exe %r (%s)", name, exe, spec["kind"])
        else:
            # Unknown name: try it verbatim as an executable.
            exe = key
            logger.info("launch_app: %r not in alias table — trying as exe", name)

        self._start_executable(exe)

    def open_url(self, url: str) -> None:
        """
        Open *url* in the user's default browser (reusing a running instance and
        opening a new tab where the platform supports it). Raises RuntimeError on
        failure.
        """
        url = url.strip()
        if not url:
            raise RuntimeError("open_url: empty URL")

        # Prefix a scheme if the model omitted it, so the browser treats the
        # argument as a navigation rather than a search query.
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url

        logger.info("open_url: opening %r in default browser", url)
        try:
            # new=2 → open in a new tab where possible; reuses the running browser.
            opened = webbrowser.open(url, new=2)
        except Exception as exc:
            raise RuntimeError(f"open_url failed for {url!r}: {exc}") from exc

        if not opened:
            raise RuntimeError(f"open_url: no browser available to open {url!r}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_executable(self, exe: str) -> None:
        """
        Start *exe* detached from this process. Tries, in order: a resolvable
        PATH entry via subprocess, then the Windows `start` shell builtin (which
        also resolves App Execution Aliases and registered app paths that are not
        on PATH). Raises RuntimeError if both fail.
        """
        resolved = shutil.which(exe)
        if resolved:
            try:
                subprocess.Popen([resolved])
                logger.info("_start_executable: launched %r", resolved)
                return
            except Exception as exc:
                logger.warning(
                    "_start_executable: Popen(%r) failed: %s — falling back to 'start'",
                    resolved, exc,
                )

        # Fallback: the cmd `start` builtin. shell=True is required for the
        # builtin; the empty "" is start's window-title argument so a quoted
        # target is not misread as the title.
        try:
            subprocess.Popen(f'start "" {exe}', shell=True)
            logger.info("_start_executable: launched %r via 'start'", exe)
            return
        except Exception as exc:
            raise RuntimeError(f"Could not launch {exe!r}: {exc}") from exc
