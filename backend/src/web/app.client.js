// Emotes Downloader dashboard (runs in the browser). Plain JS, no libraries.
// Everything from Discord (server names, emoji names, user names) is shown with textContent, never innerHTML.
(function () {
  "use strict";
  var CFG = JSON.parse(document.getElementById("config").textContent || "{}");
  var main = document.getElementById("main");
  var nav = document.getElementById("nav");
  var ME = null;

  // ------------------------------------------------------------ tiny DOM helper
  function el(tag, attrs) {
    var e = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") e.textContent = v;
      else if (k === "class") e.className = v;
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      if (Array.isArray(c)) c.forEach(function (x) { if (x) e.appendChild(x); });
      else e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function show() { clear(main); for (var i = 0; i < arguments.length; i++) if (arguments[i]) main.appendChild(arguments[i]); }

  var toastTimer = null;
  function toast(msg, bad) {
    var t = document.getElementById("toast");
    t.textContent = msg;
    t.className = "toast" + (bad ? " err" : "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, 4000);
  }

  // ------------------------------------------------------------ API
  function api(method, path, body, isForm) {
    var opts = { method: method, headers: {}, credentials: "same-origin" };
    if (body !== undefined) {
      if (isForm) opts.body = body;
      else { opts.headers["content-type"] = "application/json"; opts.body = JSON.stringify(body); }
    }
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (r.status === 401) { ME = null; navTo("/"); throw new Error(data.error || "Please log in."); }
        if (!r.ok) {
          var err = new Error(data.error || ("Error " + r.status));
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function icon(g, size) {
    var box = el("div", { class: "gicon" });
    if (g.icon) {
      box.appendChild(el("img", { src: "https://cdn.discordapp.com/icons/" + g.id + "/" + g.icon + ".png?size=" + (size || 64), alt: "" }));
    } else {
      box.textContent = (g.name || "?").split(/\s+/).map(function (w) { return w.charAt(0); }).join("").slice(0, 3);
    }
    return box;
  }

  function avatarUrl(u) {
    if (u.avatar) return "https://cdn.discordapp.com/avatars/" + u.id + "/" + u.avatar + ".png?size=64";
    return "https://cdn.discordapp.com/embed/avatars/" + (Number(BigInt(u.id) >> 22n) % 6) + ".png";
  }

  // ------------------------------------------------------------ navigation
  function navTo(path) {
    if (location.pathname !== path) history.pushState({}, "", path);
    render();
  }
  window.addEventListener("popstate", render);
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[data-nav]");
    if (a && !e.ctrlKey && !e.metaKey && e.button === 0) { e.preventDefault(); navTo(a.getAttribute("href")); }
  });

  function renderNav() {
    clear(nav);
    if (ME) {
      nav.appendChild(el("a", { href: "/dashboard", "data-nav": true, class: "btn ghost small" }, "My servers"));
      nav.appendChild(el("span", { class: "me" }, el("img", { src: avatarUrl(ME), alt: "" }), el("span", { text: ME.name })));
      nav.appendChild(el("a", { href: "/login?switch=1", class: "btn ghost small", title: "Log in with another Discord account" }, "Switch account"));
      nav.appendChild(el("button", { class: "btn ghost small", onclick: function () {
        api("POST", "/logout").then(function () { ME = null; navTo("/"); });
      } }, "Log out"));
    } else {
      nav.appendChild(el("a", { href: "/login", class: "btn discord small" }, "Log in with Discord"));
    }
  }

  function loadMe() {
    return fetch("/api/me", { credentials: "same-origin" }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (me) { ME = me; renderNav(); return me; }).catch(function () { ME = null; renderNav(); });
  }

  function render() {
    var p = location.pathname.replace(/\/+$/, "") || "/";
    if (CFG.error) { show(el("div", { class: "card center pad" }, el("p", { class: "bad", text: CFG.error }),
      el("a", { href: "/", class: "btn ghost" }, "Back to the start"))); CFG.error = ""; return; }
    if (p === "/privacy") return legal("privacy");
    if (p === "/terms") return legal("terms");
    if (p === "/dashboard") return ME ? dashboard() : landing(true);
    var m = /^\/dashboard\/(\d{15,21})$/.exec(p);
    if (m) return ME ? server(m[1]) : landing(true);
    return landing(false);
  }

  // ------------------------------------------------------------ landing
  function landing(needLogin) {
    var feat = function (t, d) { return el("div", { class: "card" }, el("h3", { text: t }), el("p", { text: d })); };
    show(
      el("section", { class: "hero" },
        el("h1", null, "Your 7TV emotes, ", el("span", { text: "in your Discord server" }), " in seconds"),
        el("p", { text: needLogin ? "Log in with Discord to see your servers." :
          "Log in with Discord, pick a server and add emotes from 7TV. Or use /emote add in your server. Free, and the bot only touches emojis and stickers." }),
        el("div", { class: "row" },
          ME ? el("a", { href: "/dashboard", "data-nav": true, class: "btn big" }, "Open my servers")
             : el("a", { href: "/login", class: "btn discord big" }, "Log in with Discord"),
          el("a", { href: "/api/invite", class: "btn ghost big", id: "addbot" }, "Add the bot to a server")),
        el("p", { class: "count", id: "count", hidden: true })),
      el("section", { class: "features" },
        feat("Only your servers", "You only see servers where you can add emojis. Nobody else sees yours."),
        feat("Add from 7TV", "Paste a 7TV emote link, or type /emote add in Discord. Wide emotes are cut into pieces like :KEKW1: :KEKW2:."),
        feat("Manage emojis", "See every emoji and free slots, rename or delete them in one place."),
        feat("Desktop app", "For big batches, stickers and your own files, get the free Windows app.")));
    liveCount();
    fetch("/api/config").then(function (r) { return r.json(); }).then(function (c) {
      var a = document.getElementById("addbot");
      if (a && c.invite) a.href = c.invite;
    });
  }

  // Live "used in N servers" counter on the landing page (refreshes every 30 seconds while it's open).
  var countTimer = null;
  function liveCount() {
    clearTimeout(countTimer);
    var c = document.getElementById("count");
    if (!c) return;
    fetch("/api/stats").then(function (r) { return r.json(); }).then(function (d) {
      if (typeof d.servers !== "number") return;
      clear(c);
      c.appendChild(el("span", { class: "dot" }));
      c.appendChild(el("b", { text: d.servers.toLocaleString("en-US") }));
      c.appendChild(document.createTextNode(d.servers === 1 ? " server uses the bot" : " servers use the bot"));
      c.hidden = false;
    }).catch(function () {}).then(function () {
      if (document.getElementById("count")) countTimer = setTimeout(liveCount, 30000);
    });
  }

  // ------------------------------------------------------------ my servers
  function dashboard() {
    show(el("p", { class: "muted center pad", text: "Loading your servers…" }));
    var go = function (fresh) {
      api("GET", "/api/guilds" + (fresh ? "?fresh=1" : "")).then(function (d) {
        var ready = el("div", { class: "servers" });
        d.ready.forEach(function (g) {
          ready.appendChild(el("a", { class: "server", href: "/dashboard/" + g.id, "data-nav": true }, icon(g),
            el("div", { class: "grow" }, el("div", { class: "name", text: g.name }), el("div", { class: "sub", text: "Manage emotes →" }))));
        });
        if (!d.ready.length) ready.appendChild(el("div", { class: "empty", text: "The bot isn't in any of your servers yet. Invite it below." }));
        var inv = el("div", { class: "servers" });
        d.invite.forEach(function (g) {
          inv.appendChild(el("div", { class: "server off" }, icon(g),
            el("div", { class: "grow" }, el("div", { class: "name", text: g.name }), el("div", { class: "sub", text: "The bot isn't here yet" })),
            el("a", { class: "btn discord small", href: g.invite, target: "_blank", rel: "noopener" }, "Invite")));
        });
        if (!d.invite.length) inv.appendChild(el("div", { class: "empty", text: "No other servers where you can add bots." }));
        show(
          el("div", { class: "tabs" }, el("h2", { text: "Your servers" }), el("span", { class: "grow" }),
            el("button", { onclick: function () { go(true); } }, "Refresh")),
          ready, el("h2", { text: "Invite the bot" }), inv,
          el("p", { class: "faint", text: "After inviting the bot, come back here. The list refreshes by itself." }));
      }).catch(function (e) { show(el("div", { class: "card center pad" }, el("p", { class: "bad", text: e.message }))); });
    };
    go(false);
    window.onfocus = function () { if (location.pathname === "/dashboard") go(true); };
  }

  // ------------------------------------------------------------ one server
  function server(gid) {
    window.onfocus = null;
    show(el("p", { class: "muted center pad", text: "Loading the server…" }));
    api("GET", "/api/guilds/" + gid).then(function (g) {
      var state = { tab: "emojis", q: "" };
      var bar = function (label, used, limit) {
        var pct = Math.min(100, Math.round(used * 100 / Math.max(1, limit)));
        return el("div", { class: "card bar" + (used >= limit ? " full" : "") },
          el("div", { class: "label" }, el("span", { text: label }), el("span", { text: used + " / " + limit })),
          el("div", { class: "track" }, (function () { var f = el("div", { class: "fill" }); f.style.width = pct + "%"; return f; })()));
      };
      var result = el("div", { class: "result" });
      var addLine = function (ok, text) {
        result.insertBefore(el("div", { class: ok ? "good" : "bad", text: (ok ? "✓ " : "✗ ") + text }), result.firstChild);
      };
      var link = el("input", { type: "text", placeholder: "Paste a 7TV emote link, e.g. https://7tv.app/emotes/…" });
      var nameIn = el("input", { type: "text", placeholder: "Name (optional)", maxlength: "32" });
      var cutBox = el("input", { type: "checkbox", checked: true });
      var cutRow = el("label", { class: "check" }, cutBox, " Cut wide emotes into pieces (:name1: :name2: …)");
      var addBtn = el("button", { class: "btn", onclick: function () {
        if (!link.value.trim()) { link.focus(); return; }
        addBtn.disabled = true; addBtn.textContent = "Adding…";
        api("POST", "/api/guilds/" + gid + "/7tv", { link: link.value.trim(), name: nameIn.value.trim() || undefined, cut: cutBox.checked })
          .then(function (r) {
            var names = (r.emojis || [r.emoji]).map(function (e) { return ":" + e.name + ":"; });
            if (r.pieces > 1) {
              addLine(true, "Added in " + r.pieces + " pieces: " + names.join(" ") + ". Type them together to show the whole emote.");
            } else {
              addLine(true, "Added " + names[0] + (r.note ? ". " + r.note : ""));
            }
            if (r.error) addLine(false, "Only " + names.length + " of " + r.pieces + " pieces were added: " + r.error);
            link.value = ""; nameIn.value = ""; reload();
          })
          .catch(function (e) { addLine(false, e.message); })
          .then(function () { addBtn.disabled = false; addBtn.textContent = "Add emote"; });
      } }, "Add emote");
      link.addEventListener("keydown", function (e) { if (e.key === "Enter") addBtn.click(); });

      // upload your own files (≤ 256 KB emoji)
      var fileIn = el("input", { type: "file", accept: "image/png,image/gif,image/jpeg,image/webp", multiple: true, hidden: true });
      var drop = el("div", { class: "drop", onclick: function () { fileIn.click(); } },
        "Or drop image files here (PNG, GIF, JPEG or WebP, 256 KB max each). Use the desktop app to shrink bigger ones.");
      var upload = function (files) {
        Array.prototype.forEach.call(files, function (f) {
          var name = f.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_]/g, "_").replace(/_+/g, "_").replace(/^_|_$/g, "").slice(0, 32) || "emote";
          if (name.length < 2) name += "_";
          if (f.size > 256 * 1024) { addLine(false, f.name + " is " + Math.round(f.size / 1024) + " KB. Emojis must be 256 KB or smaller."); return; }
          var rd = new FileReader();
          rd.onload = function () {
            api("POST", "/api/guilds/" + gid + "/emojis", { name: name, image: rd.result })
              .then(function (r) { addLine(true, "Added :" + r.emoji.name + ":"); reload(); })
              .catch(function (e) { addLine(false, ":" + name + ": " + e.message); });
          };
          rd.readAsDataURL(f);
        });
      };
      fileIn.addEventListener("change", function () { upload(fileIn.files); fileIn.value = ""; });
      ["dragenter", "dragover"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.add("over"); }); });
      ["dragleave", "drop"].forEach(function (t) { drop.addEventListener(t, function (e) { e.preventDefault(); drop.classList.remove("over"); }); });
      drop.addEventListener("drop", function (e) { upload(e.dataTransfer.files); });

      var listBox = el("div", { class: "grid" });
      var search = el("input", { type: "search", placeholder: "Search…", oninput: function () { state.q = search.value.toLowerCase(); draw(); } });
      var tabs = el("div", { class: "tabs" });
      var mkTab = function (id, label) {
        return el("button", { class: state.tab === id ? "on" : "", onclick: function () { state.tab = id; drawTabs(); draw(); } }, label);
      };
      var drawTabs = function () {
        clear(tabs);
        tabs.appendChild(mkTab("emojis", "Emojis (" + g.emojis.length + ")"));
        tabs.appendChild(mkTab("stickers", "Stickers (" + g.stickers.length + ")"));
        tabs.appendChild(mkTab("history", "History"));
        tabs.appendChild(el("span", { class: "grow" }));
        if (state.tab !== "history") tabs.appendChild(search);
      };
      var draw = function () {
        clear(listBox);
        listBox.className = state.tab === "history" ? "" : "grid";
        if (state.tab === "history") {
          listBox.appendChild(el("p", { class: "muted", text: "Loading…" }));
          api("GET", "/api/guilds/" + gid + "/history").then(function (h) {
            clear(listBox);
            if (!h.items.length) { listBox.appendChild(el("div", { class: "empty", text: "Nothing added with Emotes Downloader yet." })); return; }
            var t = el("table", { class: "hist card" });
            h.items.forEach(function (r) {
              t.appendChild(el("tr", null,
                el("td", { class: r.ok ? "good" : "bad", text: r.ok ? "✓" : "✗" }),
                el("td", { text: (r.kind === "emoji" ? ":" + r.name + ":" : r.name) }),
                el("td", { class: "muted", text: r.user_name || "" }),
                el("td", { class: "faint", text: ({ app: "desktop app", web: "dashboard", "7tv": "7TV link", command: "/emote add" })[r.source] || r.source }),
                el("td", { class: "faint", text: r.ok ? new Date(r.created_at).toLocaleString() : (r.error || "") })));
            });
            listBox.appendChild(t);
          }).catch(function (e) { clear(listBox); listBox.appendChild(el("p", { class: "bad", text: e.message })); });
          return;
        }
        var items = state.tab === "emojis" ? g.emojis : g.stickers;
        items = items.filter(function (x) { return !state.q || x.name.toLowerCase().indexOf(state.q) >= 0; });
        if (!items.length) { listBox.appendChild(el("div", { class: "empty", text: state.q ? "Nothing matches." : "None yet." })); return; }
        items.forEach(function (x) {
          var isE = state.tab === "emojis";
          var src = isE ? "https://cdn.discordapp.com/emojis/" + x.id + "." + (x.animated ? "gif" : "png") + "?size=96"
                        : "https://media.discordapp.net/stickers/" + x.id + ".png?size=96";
          var acts = g.can_manage ? el("div", { class: "acts" },
            el("button", { class: "btn ghost small", onclick: function () { rename(isE, x); } }, "Rename"),
            el("button", { class: "btn danger small", onclick: function () { remove(isE, x); } }, "Delete")) : null;
          listBox.appendChild(el("div", { class: "emo", title: isE ? ":" + x.name + ":" : x.name },
            el("img", { src: src, alt: x.name, loading: "lazy" }), el("div", { class: "n", text: x.name }), acts));
        });
      };
      var rename = function (isE, x) {
        var n = prompt("New name" + (isE ? " (letters, numbers or _, 2–32 characters)" : " (2–30 characters)"), x.name);
        if (!n || n === x.name) return;
        api("PATCH", "/api/guilds/" + gid + "/" + (isE ? "emojis" : "stickers") + "/" + x.id, { name: n.trim() })
          .then(function () { toast("Renamed to " + n.trim()); reload(); })
          .catch(function (e) { toast(e.message, true); });
      };
      var remove = function (isE, x) {
        if (!confirm("Delete " + (isE ? ":" + x.name + ":" : x.name) + " from " + g.name + "? This can't be undone.")) return;
        api("DELETE", "/api/guilds/" + gid + "/" + (isE ? "emojis" : "stickers") + "/" + x.id)
          .then(function () { toast("Deleted " + x.name); reload(); })
          .catch(function (e) { toast(e.message, true); });
      };
      var bars = el("div", { class: "bars" });
      var drawBars = function () {
        clear(bars);
        bars.appendChild(bar("Emojis", g.static, g.limit));
        bars.appendChild(bar("Animated emojis", g.animated, g.limit));
        bars.appendChild(bar("Stickers", g.stickers.length, g.sticker_limit));
      };
      var reload = function () {
        api("GET", "/api/guilds/" + gid).then(function (n) { g = n; drawBars(); drawTabs(); draw(); }).catch(function () {});
      };
      drawBars(); drawTabs(); draw();
      show(
        el("div", { class: "ghead" }, icon(g, 128),
          el("div", { class: "grow" }, el("h1", { text: g.name }),
            el("div", { class: "muted", text: "Boost level " + g.tier + (g.can_manage ? "" : " · you can add emojis, but not rename or delete them") })),
          el("a", { href: "/dashboard", "data-nav": true, class: "btn ghost" }, "← All servers")),
        bars,
        el("h2", { text: "Add emotes" }),
        el("div", { class: "card" }, el("div", { class: "addrow" }, link, nameIn, addBtn), cutRow, drop, fileIn, result),
        tabs, listBox);
    }).catch(function (e) {
      show(el("div", { class: "card center pad" }, el("p", { class: "bad", text: e.message }),
        el("a", { href: "/dashboard", "data-nav": true, class: "btn ghost" }, "← All servers")));
    });
  }

  // ------------------------------------------------------------ privacy / terms
  function legal(which) {
    var P = function (t) { return el("p", { text: t }); };
    var H = function (t) { return el("h3", { text: t }); };
    if (which === "privacy") {
      show(el("article", { class: "legal" }, el("h1", { text: "Privacy Policy" }), P("Last updated: November 2026."),
        H("What we store"),
        P("When you log in with Discord we store your Discord user ID, name and avatar, and your Discord login tokens (encrypted). We use them only to show you your own servers and to check your permissions."),
        P("When you add, rename or delete an emoji or sticker with Emotes Downloader we keep a log (server ID, your user ID and name, the emoji name, when, and whether it worked) for 90 days, so server staff can see who added what."),
        H("What we don't do"),
        P("We don't read messages, we don't sell or share your data, and we don't use cookies for tracking. The only cookie is your login."),
        H("Deleting your data"),
        P("Log out to delete your login right away. Logins you don't use for 30 days are deleted automatically. To remove everything, kick the bot from your server and contact us on GitHub."),
        H("Contact"), el("p", null, el("a", { href: "https://github.com/ISdook/emotes-downloader/issues", text: "github.com/ISdook/emotes-downloader" }))));
    } else {
      show(el("article", { class: "legal" }, el("h1", { text: "Terms of Service" }), P("Last updated: November 2026."),
        H("Using Emotes Downloader"),
        P("Emotes Downloader is a free tool that adds emotes and stickers to Discord servers where you have permission to manage them. You must follow Discord's Terms of Service and Community Guidelines and only add content you're allowed to use."),
        H("No guarantees"),
        P("The service is provided as is, without warranties. It may change or stop at any time. We're not responsible for emotes added, renamed or deleted with it."),
        H("Limits"),
        P("To keep the service working for everyone, each user can add a limited number of emotes per hour, and abuse can lead to being blocked."),
        H("Contact"), el("p", null, el("a", { href: "https://github.com/ISdook/emotes-downloader/issues", text: "github.com/ISdook/emotes-downloader" }))));
    }
  }

  loadMe().then(render);
})();
