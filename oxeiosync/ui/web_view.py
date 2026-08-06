"""The embedded Syncthing web UI.

Syncthing's own interface is the real configuration surface, so rather than
reimplementing it, oXeioSync hosts it in a Chromium view. Four things make the
embedded copy behave better than a browser tab:

* every request carries the API key, so the UI is authorised even if the user
  has put a password on the GUI;
* links that leave Syncthing open in the real browser instead of stranding the
  user inside the app;
* when Syncthing isn't up yet, a placeholder is shown and the page is reloaded
  automatically once it is;
* the folder path field gets a Browse button that opens the system's folder
  picker — a web page cannot do that, but the desktop application hosting it
  can.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from string import Template

from PySide6.QtCore import QDir, QEvent, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget

from .. import APP_NAME, paths
from .theme import Palette, palette_for

log = logging.getLogger(__name__)

#: Message the injected Browse button passes to ``window.prompt`` to ask the
#: host application for a folder. ``prompt`` is the one call a page can make
#: that reaches native code and waits for an answer, which is exactly the shape
#: a file dialog needs; the page below sends nothing else through it.
PICK_FOLDER_REQUEST = "oxeiosync:pick-folder"


class _ApiKeyInterceptor(QWebEngineUrlRequestInterceptor):
    """Adds ``X-API-Key`` to every request aimed at the Syncthing GUI.

    Scoped to the GUI's own origin so the key is never leaked to a third party
    if the embedded UI ever loads something external.
    """

    def __init__(self, api_key: str, allowed_host: str, allowed_port: int) -> None:
        super().__init__()
        self._api_key = api_key
        self._allowed_host = allowed_host
        self._allowed_port = allowed_port

    def reconfigure(self, api_key: str, allowed_host: str, allowed_port: int) -> None:
        self._api_key = api_key
        self._allowed_host = allowed_host
        self._allowed_port = allowed_port

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:  # noqa: N802
        url = info.requestUrl()
        if url.host() != self._allowed_host:
            return
        if self._allowed_port and url.port(80) != self._allowed_port:
            return
        if self._api_key:
            info.setHttpHeader(b"X-API-Key", self._api_key.encode("ascii"))


class _SyncthingPage(QWebEnginePage):
    """Keeps navigation inside Syncthing, handing anything else to the browser."""

    def __init__(self, profile: QWebEngineProfile, allowed_host: str, parent: QWidget) -> None:
        super().__init__(profile, parent)
        self._allowed_host = allowed_host
        self._host_widget = parent
        self.newWindowRequested.connect(self._on_new_window_requested)

    def set_allowed_host(self, host: str) -> None:
        self._allowed_host = host

    def acceptNavigationRequest(  # noqa: N802
        self,
        url: QUrl,
        nav_type: QWebEnginePage.NavigationType,
        is_main_frame: bool,
    ) -> bool:
        if url.scheme() in ("http", "https") and url.host() != self._allowed_host:
            log.debug("Opening %s in the system browser", url.toString())
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def _on_new_window_requested(self, request: object) -> None:
        # Syncthing's UI opens documentation and release notes in new tabs;
        # there is no tab strip here, so send them to the real browser.
        url = getattr(request, "requestedUrl", None)
        if callable(url):
            QDesktopServices.openUrl(url())

    def javaScriptPrompt(  # noqa: N802
        self,
        security_origin: QUrl,
        message: str,
        default_value: str,
    ) -> tuple[bool, str]:
        """Answer the Browse button with a native folder picker.

        The renderer is blocked while this runs, which is what makes the result
        usable as the ``prompt`` return value — the button can then write the
        chosen path straight into the field.
        """
        if message != PICK_FOLDER_REQUEST:
            return super().javaScriptPrompt(security_origin, message, default_value)
        # Only the configuration page may ask. Nothing else is ever loaded here,
        # but the check keeps that true if something ever is.
        if security_origin.host() != self._allowed_host:
            log.debug("Ignoring a folder request from %s", security_origin.toString())
            return False, ""

        chosen = QFileDialog.getExistingDirectory(
            self._host_widget,
            "Select a Folder to Sync",
            _start_directory(default_value),
        )
        if not chosen:
            return False, ""
        # Qt hands back forward slashes on every platform; the engine stores the
        # path verbatim, so give it the one the rest of the system writes.
        return True, QDir.toNativeSeparators(chosen)

    def javaScriptConsoleMessage(  # noqa: N802
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        message: str,
        line: int,
        source: str,
    ) -> None:
        # Syncthing's UI is chatty; keep it out of the user-visible log.
        log.debug("web console [%s:%s] %s", source, line, message)


class SyncthingWebView(QWidget):
    """A Chromium view showing Syncthing's GUI, with a friendly offline state."""

    def __init__(self, base_url: str, api_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._base_url = base_url

        # A named profile persists cookies and local storage, so Syncthing's UI
        # remembers the user's theme and dismissed notices between runs.
        self._profile = QWebEngineProfile(APP_NAME, self)
        storage = paths.data_dir() / "webview"
        self._profile.setPersistentStoragePath(str(storage))
        self._profile.setCachePath(str(storage / "cache"))
        self._profile.setHttpUserAgent(f"{APP_NAME} embedded configuration view")

        url = QUrl(base_url)
        self._interceptor = _ApiKeyInterceptor(api_key, url.host(), url.port(80))
        self._profile.setUrlRequestInterceptor(self._interceptor)
        self._install_theme()
        # Unlike the theme, this one does not depend on the colour scheme, so it
        # is installed once and left alone.
        self._profile.scripts().insert(_folder_browse_script())

        self._view = QWebEngineView(self)
        self._page = _SyncthingPage(self._profile, url.host(), self._view)
        self._view.setPage(self._page)
        self._view.loadFinished.connect(self._on_load_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self._showing_placeholder = False

    # ------------------------------------------------------------------- public
    def reconfigure(self, base_url: str, api_key: str) -> None:
        """Point the view at a different Syncthing instance."""
        self._base_url = base_url
        url = QUrl(base_url)
        self._interceptor.reconfigure(api_key, url.host(), url.port(80))
        self._page.set_allowed_host(url.host())

    def load_syncthing(self) -> None:
        """Load (or reload) the configuration page."""
        self._showing_placeholder = False
        self._view.setUrl(QUrl(self._base_url))

    # -------------------------------------------------------------------- theme
    def _install_theme(self) -> None:
        """Put the current colour scheme's stylesheet on the profile."""
        scripts = self._profile.scripts()
        for existing in scripts.find("oxeiosync-rebrand"):
            scripts.remove(existing)
        scripts.insert(_rebrand_script(palette_for(self)))

    def changeEvent(self, event) -> None:  # noqa: N802
        # The injected stylesheet is baked for one colour scheme, so it has to
        # be rebuilt and the page reloaded when the host switches themes.
        if event.type() == QEvent.Type.PaletteChange:
            self._install_theme()
            if not self._showing_placeholder:
                self.load_syncthing()
        super().changeEvent(event)

    def reload_if_offline(self) -> None:
        """Retry the load, but only if we are currently showing the placeholder.

        Called when Syncthing comes up, so the user does not have to click
        anything, while an already-working page is left untouched.
        """
        if self._showing_placeholder:
            self.load_syncthing()

    def reload_after_wake(self) -> None:
        """Reload the live page after the machine has slept.

        Chromium's render surface can be lost across a display sleep — a MacBook
        lid closed and reopened — leaving the view blank until it reloads. A
        placeholder (the engine is down) is left alone: reloading it would only
        re-show the same placeholder.
        """
        if not self._showing_placeholder:
            self.load_syncthing()

    def show_placeholder(self, message: str) -> None:
        """Replace the view with a plain 'not available yet' page."""
        self._showing_placeholder = True
        self._page.setHtml(_placeholder_html(message), QUrl("about:blank"))

    # ------------------------------------------------------------------ handlers
    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self._showing_placeholder = self._view.url().scheme() == "about"
            return
        # A failure here almost always means Syncthing has not finished
        # starting, or has been stopped from the tray menu.
        self.show_placeholder(
            f"Could not reach the sync engine at {self._base_url}.<br>"
            "It may still be starting up."
        )


#: Restyles the engine's configuration page to match the rest of the
#: application: the same surfaces, ink, accent, radii and type as the dashboard,
#: in place of the stock Bootstrap look. Every control keeps working exactly as
#: before — this changes appearance only.
#:
#: ``string.Template`` rather than an f-string, because CSS is mostly braces and
#: doubling every one of them would make this unreadable.
_THEME_CSS = Template(
    """
:root { color-scheme: $scheme; }
html, body {
  background: $plane !important;
  color: $ink2 !important;
  font-size: 14px;
}
/* The page ships a display face for headings; the rest of the application uses
   one system sans, so unify on it. Targeted rather than a blanket rule on
   everything — that would hit the icon font too and turn every glyph into a
   tofu box. */
body, p, td, th, li, label, .btn, .form-control,
input, select, textarea, .navbar-text, .dropdown-menu > li > a {
  font-family: $font !important;
}
h1, h2, h3, h4, h5, h6, .panel-title, .modal-title { font-family: $font !important; }
/* Belt and braces: whatever else happens, icons keep their own family. */
.fa, .fas, .far, .fal, .fab, [class^="fa-"], [class*=" fa-"] {
  font-family: ForkAwesome !important;
}

/* --- page frame --------------------------------------------------------- */
.container { max-width: 1180px; }
body > .container { padding-top: 24px !important; padding-bottom: 48px; }

/* --- navbar ------------------------------------------------------------- */
.navbar.navbar-top {
  background: $plane !important;
  border: 0 !important;
  border-bottom: 1px solid $border !important;
  box-shadow: none !important;
  margin-bottom: 0 !important;
  min-height: 54px;
}
.navbar .navbar-brand::after { color: $ink !important; font-size: 17px !important; }
.navbar-text { color: $muted !important; font-size: 13px; }
.navbar-nav > li > a {
  color: $ink2 !important;
  font-size: 13px;
  padding: 17px 14px !important;
}
.navbar-nav > li > a:hover,
.navbar-nav > .open > a,
.navbar-nav > .open > a:focus { background: $surface !important; color: $ink !important; }

/* --- section headings --------------------------------------------------- */
h3 {
  font-size: 12px !important;
  font-weight: 600 !important;
  text-transform: uppercase;
  letter-spacing: .07em !important;
  color: $muted !important;
  margin: 28px 0 10px !important;
}

/* --- cards -------------------------------------------------------------- */
.panel, .panel-default {
  background: $surface !important;
  border: 1px solid $border !important;
  border-radius: 10px !important;
  box-shadow: none !important;
  margin-bottom: 10px !important;
  overflow: hidden;
}
.panel > .panel-heading, .panel-heading {
  background: transparent !important;
  border: 0 !important;
  border-radius: 0 !important;
  color: $ink !important;
  padding: 13px 16px !important;
  width: 100%;
  text-align: left !important;
}
.panel-title { font-size: 14px !important; font-weight: 600 !important; }
.panel-body { padding: 2px 16px 14px !important; background: transparent !important; }
.panel-footer {
  background: transparent !important;
  border-top: 1px solid $border !important;
  padding: 10px 16px !important;
}

/* --- tables ------------------------------------------------------------- */
.table { margin-bottom: 0 !important; }
.table > tbody > tr > td, .table > tbody > tr > th {
  border-top: 1px solid $border !important;
  padding: 8px 0 !important;
  background: transparent !important;
  font-weight: 400 !important;
  font-size: 13px;
  vertical-align: middle !important;
}
.table > tbody > tr:first-child > td,
.table > tbody > tr:first-child > th { border-top: 0 !important; }
.table > tbody > tr > th { color: $muted !important; }
.table > tbody > tr > td { color: $ink2 !important; }
/* Zebra striping fights the hairline rules; keep one separator, not both. */
.table-striped > tbody > tr:nth-of-type(odd),
.table-hover > tbody > tr:hover { background: transparent !important; }

/* --- buttons ------------------------------------------------------------ */
.btn {
  border-radius: 7px !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  box-shadow: none !important;
  text-shadow: none !important;
  padding: 7px 13px !important;
}
.btn-default {
  background: transparent !important;
  border: 1px solid $border !important;
  color: $ink2 !important;
}
.btn-default:hover, .btn-default:focus {
  background: $raised !important;
  border-color: $muted !important;
  color: $ink !important;
}
.btn-primary, .btn-success {
  background: $accent !important;
  border: 1px solid $accent !important;
  color: #ffffff !important;
}
.btn-primary:hover, .btn-success:hover { filter: brightness(1.12); }
.btn-danger {
  background: $critical !important;
  border-color: $critical !important;
  color: #ffffff !important;
}
.btn-warning {
  background: $warning !important;
  border-color: $warning !important;
  color: #201d16 !important;
}
.btn.panel-heading { border: 0 !important; }
.btn-group > .btn { border-radius: 7px !important; }

/* --- forms -------------------------------------------------------------- */
.form-control, select, textarea,
input[type=text], input[type=number], input[type=password], input[type=email] {
  background: $plane !important;
  border: 1px solid $border !important;
  border-radius: 7px !important;
  color: $ink !important;
  box-shadow: none !important;
}
.form-control:focus, select:focus, textarea:focus {
  border-color: $accent !important;
  box-shadow: 0 0 0 3px $accentGlow !important;
}
label, .control-label { color: $ink2 !important; font-weight: 500 !important; }
.help-block { color: $muted !important; }
.input-group-addon {
  background: $raised !important;
  border-color: $border !important;
  color: $muted !important;
  border-radius: 7px !important;
}

/* --- dropdowns and dialogs ---------------------------------------------- */
.dropdown-menu {
  background: $surface !important;
  border: 1px solid $border !important;
  border-radius: 10px !important;
  box-shadow: 0 12px 32px rgba(0,0,0,.45) !important;
  padding: 6px !important;
}
.dropdown-menu > li > a {
  color: $ink2 !important;
  border-radius: 6px !important;
  padding: 7px 12px !important;
}
.dropdown-menu > li > a:hover { background: $raised !important; color: $ink !important; }
.dropdown-menu .divider { background: $border !important; }
.modal-content {
  background: $surface !important;
  border: 1px solid $border !important;
  border-radius: 12px !important;
  box-shadow: 0 24px 64px rgba(0,0,0,.55) !important;
}
/* The page's own theme paints these dark whatever we do, which is invisible in
   dark mode and glaring in light — so state them explicitly. */
.modal-header, .modal-footer {
  background: $surface !important;
  border-color: $border !important;
  padding: 16px 20px !important;
}
.modal-title { font-size: 16px !important; font-weight: 600 !important; color: $ink !important; }
.modal-body { background: $surface !important; padding: 10px 20px 16px !important; }
.modal-backdrop.in { opacity: .6 !important; }
.nav-tabs { border-bottom: 1px solid $border !important; }
.nav-tabs > li > a,
.nav-tabs > li.active > a,
.nav-tabs > li.active > a:hover,
.nav-tabs > li.active > a:focus {
  background: transparent !important;
  border: 0 !important;
  border-radius: 0 !important;
  color: $muted !important;
  padding: 10px 14px !important;
}
.nav-tabs > li.active > a,
.nav-tabs > li.active > a:hover,
.nav-tabs > li.active > a:focus {
  color: $ink !important;
  box-shadow: inset 0 -2px 0 $accent !important;
}

/* --- odds and ends ------------------------------------------------------ */
a { color: $accent !important; }
hr { border-color: $border !important; }
.well {
  background: $plane !important;
  border: 1px solid $border !important;
  border-radius: 10px !important;
  box-shadow: none !important;
}
.text-success { color: $good !important; }
.text-warning { color: $warning !important; }
.text-danger  { color: $critical !important; }
.text-muted   { color: $muted !important; }
.progress {
  background: $plane !important;
  border-radius: 6px !important;
  box-shadow: none !important;
  height: 8px !important;
}
.progress-bar { background: $accent !important; box-shadow: none !important; }
.label-primary, .badge { background: $accent !important; border-radius: 6px !important; }
.identicon rect { fill: $ink2 !important; }
.alert {
  border-radius: 10px !important;
  border: 1px solid $border !important;
  background: $raised !important;
  color: $ink2 !important;
}

/* --- upstream branding -------------------------------------------------- */
.navbar-brand img.logo, .navbar-brand img { display: none !important; }
.navbar-brand::after {
  content: '$brand';
  font-weight: 600;
  letter-spacing: .01em;
}
a[href*='syncthing.net'], a[href*='github.com/syncthing'] { display: none !important; }
"""
)

#: Presents the configuration page as part of this application: applies the
#: theme above, hides the upstream logo and wordmark, and renames the project in
#: any text the page renders.
_REBRAND_SCRIPT = Template(
    r"""
(function () {
  var BRAND = "$brand";
  /* Two regexes on purpose: a /g regex carries lastIndex across .test() calls,
     so reusing one for both testing and replacing would match every other
     node. */
  var ENGINE_TEST = /Syncthing/;
  var ENGINE_ALL = /Syncthing/g;

  var style = document.createElement("style");
  style.id = "oxeiosync-theme";
  style.textContent = $css;
  (document.head || document.documentElement).appendChild(style);

  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, NOSCRIPT: 1 };

  function renameTextNodes(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        if (node.parentNode && SKIP[node.parentNode.nodeName]) {
          return NodeFilter.FILTER_REJECT;
        }
        return ENGINE_TEST.test(node.nodeValue)
          ? NodeFilter.FILTER_ACCEPT
          : NodeFilter.FILTER_REJECT;
      }
    });
    var pending = [];
    while (walker.nextNode()) pending.push(walker.currentNode);
    for (var i = 0; i < pending.length; i++) {
      pending[i].nodeValue = pending[i].nodeValue.replace(ENGINE_ALL, BRAND);
    }
  }

  var BRAND_LINK = /syncthing\.net|github\.com\/syncthing/;

  /* Hide the whole menu row, not just the anchor inside it — hiding only the
     link leaves an empty sliver behind that still takes up space. The upstream
     About dialog goes too: it is the one screen built entirely around the other
     project's identity. */
  function hideBrandedItems() {
    var links = document.querySelectorAll(".dropdown-menu a[href], .dropdown-menu a[ng-click]");
    for (var i = 0; i < links.length; i++) {
      var href = links[i].getAttribute("href") || "";
      var action = links[i].getAttribute("ng-click") || "";
      if (!BRAND_LINK.test(href) && !/^about\(/.test(action)) continue;
      var row = links[i].closest ? links[i].closest("li") : links[i].parentNode;
      if (row) row.style.display = "none";
    }
  }

  /* The Help menu is upstream documentation and an upstream About dialog from
     top to bottom, so the whole menu goes. Identified by the links it contains
     rather than by counting what survived: an emptied menu is worse than no
     menu, and a closed dropdown's items all measure as invisible, so anything
     layout-based would take Actions — and Show ID with it. */
  function hideBrandedMenus() {
    var menus = document.querySelectorAll(".navbar-nav > li.dropdown");
    for (var i = 0; i < menus.length; i++) {
      if (menus[i].querySelector('a[href*="syncthing"]')) {
        menus[i].style.display = "none";
      }
    }
  }

  function apply() {
    document.title = BRAND;
    renameTextNodes(document.body || document.documentElement);
    hideBrandedItems();
    hideBrandedMenus();
  }

  apply();

  /* The page is an Angular app that re-renders constantly, so a single pass
     is not enough. Coalesce bursts of mutations into one rewrite per frame. */
  var queued = false;
  new MutationObserver(function () {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () {
      queued = false;
      apply();
    });
  }).observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true
  });
})();
"""
)


#: Puts a Browse button beside the folder path field of the folder dialog and
#: fills the field in with whatever the user picks.
#:
#: The field itself is left exactly as it is — still typeable, still driven by
#: the page's own model — so the button is an addition to the dialog rather than
#: a replacement for any part of it. Writing to the field ends with an ``input``
#: event because the page watches for one; assigning the value alone would show
#: the path on screen and save the old one.
_FOLDER_BROWSE_SCRIPT = Template(
    r"""
(function () {
  var REQUEST = $request;
  var LABEL = $label;
  var FIELD_ID = "folderPath";
  var ROW_CLASS = "oxeiosync-browse-row";

  function pick(input) {
    if (input.readOnly || input.disabled) return;
    /* Blocks until the host application's folder dialog closes; null means the
       user cancelled, and the field must then keep what it already had. */
    var chosen = window.prompt(REQUEST, input.value || "");
    if (!chosen) return;
    input.value = chosen;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function makeButton(input) {
    var button = document.createElement("button");
    /* Inside a form, a button with no type submits it. */
    button.type = "button";
    button.className = "btn btn-default";
    button.style.whiteSpace = "nowrap";
    button.innerHTML = '<i class="fa fa-folder-open"></i> ';
    button.appendChild(document.createTextNode(LABEL));
    button.addEventListener("click", function (event) {
      event.preventDefault();
      pick(input);
    });
    return button;
  }

  function attach() {
    var input = document.getElementById(FIELD_ID);
    if (!input || !input.parentNode) return;

    var row = input.parentNode;
    if (!row.classList || !row.classList.contains(ROW_CLASS)) {
      /* The page re-renders this dialog, which drops the field back beside its
         label; wrap it again rather than assuming one pass is enough. */
      row = document.createElement("div");
      row.className = ROW_CLASS;
      row.style.display = "flex";
      row.style.alignItems = "center";
      row.style.gap = "8px";
      input.parentNode.insertBefore(row, input);
      row.appendChild(input);
      /* min-width, or the field refuses to shrink below its natural size and
         pushes the button off the edge of the dialog. */
      input.style.flex = "1 1 auto";
      input.style.minWidth = "0";
      row.appendChild(makeButton(input));
    }

    /* The path of an existing folder cannot be changed, and the page marks the
       field read-only to say so. Follow it rather than offering a picker whose
       result would be thrown away. */
    var button = row.querySelector("button");
    if (button) button.disabled = input.readOnly || input.disabled;
  }

  attach();

  /* The dialog is built, re-rendered and marked read-only long after load, so
     one pass is not enough. A short timer rather than a frame callback: frames
     stop being scheduled while the page is not on screen, and the dialog can be
     opened on the very first frame after switching back to it. */
  var queued = false;
  new MutationObserver(function () {
    if (queued) return;
    queued = true;
    setTimeout(function () {
      queued = false;
      attach();
    }, 50);
  }).observe(document.documentElement, { childList: true, subtree: true, attributes: true });
})();
"""
)


def _folder_browse_script() -> QWebEngineScript:
    """The Browse button, ready to install on a profile."""
    source = _FOLDER_BROWSE_SCRIPT.substitute(
        request=json.dumps(PICK_FOLDER_REQUEST),
        label=json.dumps("Browse…"),
    )
    script = QWebEngineScript()
    script.setName("oxeiosync-folder-browse")
    script.setSourceCode(source)
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
    script.setRunsOnSubFrames(False)
    return script


def _start_directory(current: str) -> str:
    """Where the folder picker should open, given what is in the path field.

    The field holds whatever the user has typed so far, which may be nothing, a
    tilde shortcut, or a path that does not exist yet. Walk up to the nearest
    directory that does exist, so the dialog opens somewhere related to the
    answer instead of wherever the process last happened to look.
    """
    text = (current or "").strip()
    if text.startswith("~"):
        text = str(Path.home()) + text[1:]
    if not text:
        return str(Path.home())
    try:
        candidate = Path(text)
        while candidate != candidate.parent and not candidate.is_dir():
            candidate = candidate.parent
        # Absolute, or it is worthless: walking a nonsense path up bottoms out at
        # the relative "." (the process's cwd), and opening the picker there is
        # exactly the "wherever it last looked" this exists to avoid. On Python
        # 3.12 is_dir() no longer raises on an embedded null byte — it returns
        # False — so an absolute check is what makes this reject junk, not luck.
        if candidate.is_dir() and candidate.is_absolute():
            return str(candidate)
    except (OSError, ValueError) as exc:  # A malformed path is not worth a crash.
        log.debug("Cannot resolve %r as a starting directory: %s", current, exc)
    return str(Path.home())


def _theme_css(palette: Palette) -> str:
    """The stylesheet for one colour scheme."""
    accent = QColor(palette.series_download)
    glow = f"rgba({accent.red()},{accent.green()},{accent.blue()},0.25)"
    return _THEME_CSS.substitute(
        scheme="dark" if palette.is_dark else "light",
        plane=palette.plane,
        surface=palette.surface,
        raised=palette.raised,
        border=palette.border,
        ink=palette.ink,
        ink2=palette.ink_secondary,
        muted=palette.ink_muted,
        accent=palette.series_download,
        accentGlow=glow,
        good=palette.status_good,
        warning=palette.status_warning,
        critical=palette.status_critical,
        font='system-ui, -apple-system, "Segoe UI", sans-serif',
        brand=APP_NAME,
    )


def _rebrand_script(palette: Palette) -> QWebEngineScript:
    """The theme-and-rebrand script, ready to install on a profile."""
    source = _REBRAND_SCRIPT.substitute(
        brand=APP_NAME,
        # json.dumps produces a correctly escaped JavaScript string literal, so
        # quotes and newlines in the CSS cannot break out of it.
        css=json.dumps(_theme_css(palette)),
    )
    script = QWebEngineScript()
    script.setName("oxeiosync-rebrand")
    script.setSourceCode(source)
    # DocumentReady, so document.body exists and the app's own scripts have not
    # yet painted their first frame.
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
    # An isolated world still sees the DOM but cannot collide with the page's
    # own variables.
    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
    script.setRunsOnSubFrames(False)
    return script


def _placeholder_html(message: str) -> str:
    """A self-contained page used when the real UI cannot be loaded.

    Painted from the application's own tokens rather than the browser's system
    colours: this page fills a tab in a window that is dark by decision, and
    Chromium would otherwise resolve ``Canvas`` from whatever the platform is
    set to — a white panel in a dark window, at the exact moment something has
    already gone wrong.
    """
    palette = palette_for()
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{APP_NAME}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; height: 100vh; display: flex; align-items: center;
    justify-content: center; font-family: "Segoe UI", system-ui, sans-serif;
    background: {palette.plane}; color: {palette.ink};
  }}
  .card {{ max-width: 30rem; padding: 2rem; text-align: center; }}
  h1 {{ font-size: 1.25rem; font-weight: 600; margin: 0 0 .75rem; }}
  p {{ margin: 0; color: {palette.ink_secondary}; line-height: 1.5; }}
</style></head>
<body><div class="card">
  <h1>Waiting for the sync engine</h1>
  <p>{message}</p>
</div></body></html>"""
