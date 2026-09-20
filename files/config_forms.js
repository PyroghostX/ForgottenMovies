(function () {
  function showResult(el, ok, message) {
    if (!el) return;
    el.textContent = message;
    el.style.color = ok ? "#c8f7d4" : "#ff8a80";
  }

  function postForm(url, form, extra) {
    var data = form ? new FormData(form) : new FormData();
    if (extra) {
      Object.keys(extra).forEach(function (k) { data.set(k, extra[k]); });
    }
    return fetch(url, {
      method: "POST",
      body: data,
      headers: { "Accept": "application/json", "X-Requested-With": "fetch" }
    }).then(function (r) {
      return r.json().then(function (d) { return { ok: r.ok, data: d }; });
    });
  }

  document.addEventListener("click", function (ev) {
    var connBtn = ev.target.closest(".test-connection-btn");
    if (connBtn) {
      ev.preventDefault();
      var form = connBtn.closest("form");
      var row = connBtn.closest(".test-row");
      var result = row ? row.querySelector('.test-result[data-for="connection"]') : null;
      var service = connBtn.getAttribute("data-service");
      connBtn.disabled = true;
      showResult(result, true, "Testing " + service + "…");
      postForm("/settings/test-connection", form, { service: service })
        .then(function (res) {
          showResult(result, res.data.ok, res.data.message || (res.data.ok ? "OK" : "Failed"));
        })
        .catch(function () { showResult(result, false, "Request failed."); })
        .finally(function () { connBtn.disabled = false; });
      return;
    }

    var plexBtn = ev.target.closest(".plex-signin-btn");
    if (plexBtn) {
      ev.preventDefault();
      var row3 = plexBtn.closest(".test-row");
      var result3 = row3 ? row3.querySelector('.test-result[data-for="connection"]') : null;
      var popup = window.open("", "_blank");
      plexBtn.disabled = true;
      showResult(result3, true, "Opening Plex sign-in…");
      postForm("/settings/plex-pin/start", null, {})
        .then(function (res) {
          if (!res.data.ok) { throw new Error(res.data.message || "Could not start sign-in."); }
          if (popup) { popup.location = res.data.auth_url; } else { window.location = res.data.auth_url; }
          showResult(result3, true, "Waiting for you to approve in Plex…");
          var tries = 0;
          var poll = function () {
            tries += 1;
            fetch("/settings/plex-pin/poll/" + res.data.pin_id, { headers: { "Accept": "application/json", "X-Requested-With": "fetch" } })
              .then(function (r) { return r.json(); })
              .then(function (d) {
                if (d.done) {
                  showResult(result3, d.ok, d.message || (d.ok ? "Signed in." : "Failed."));
                  plexBtn.disabled = false;
                  var tokenField = document.getElementById("f_PLEX_TOKEN");
                  if (d.ok && tokenField) { tokenField.placeholder = "•••••••• (saved by Plex sign-in)"; }
                  return;
                }
                if (tries < 120) { setTimeout(poll, 2500); } else { showResult(result3, false, "Timed out waiting for Plex."); plexBtn.disabled = false; }
              })
              .catch(function () { showResult(result3, false, "Sign-in check failed."); plexBtn.disabled = false; });
          };
          setTimeout(poll, 2500);
        })
        .catch(function (err) {
          if (popup) { popup.close(); }
          showResult(result3, false, err.message || "Request failed.");
          plexBtn.disabled = false;
        });
      return;
    }

    var emailBtn = ev.target.closest(".test-email-btn");
    if (emailBtn) {
      ev.preventDefault();
      var form2 = emailBtn.closest("form");
      var row2 = emailBtn.closest(".test-row");
      var result2 = row2 ? row2.querySelector('.test-result[data-for="email"]') : null;
      var recipientInput = row2 ? row2.querySelector(".test-email-input") : null;
      var recipient = recipientInput ? recipientInput.value.trim() : "";
      emailBtn.disabled = true;
      showResult(result2, true, "Sending…");
      postForm("/settings/test-email", form2, { test_email: recipient })
        .then(function (res) {
          showResult(result2, res.data.ok, res.data.message || (res.data.ok ? "Sent" : "Failed"));
        })
        .catch(function () { showResult(result2, false, "Request failed."); })
        .finally(function () { emailBtn.disabled = false; });
      return;
    }
  });

  // Client-side password-match guard for the setup wizard.
  document.addEventListener("submit", function (ev) {
    var form = ev.target;
    var pw = form.querySelector('input[name="admin_password"]');
    var confirm = form.querySelector('input[name="admin_password_confirm"]');
    if (pw && confirm && pw.value !== confirm.value) {
      ev.preventDefault();
      confirm.setCustomValidity("Passwords do not match.");
      confirm.reportValidity();
      setTimeout(function () { confirm.setCustomValidity(""); }, 100);
    }
  });
})();
