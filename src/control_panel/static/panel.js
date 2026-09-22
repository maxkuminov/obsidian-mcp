/* Panel behaviour, delegated from `data-*` attributes.

   The panel runs under a nonce-based Content-Security-Policy (see
   docs/architecture/control-panel.md), which refuses inline event handlers.
   Every control therefore declares what it does with a `data-*` attribute and
   this one file, loaded by base.html with the response nonce, installs
   document-level listeners that act on them. A new control is a `data-*`
   attribute plus a branch here — never an `on*=` attribute.

   Attribute values are read as plain strings or element ids. Nothing here
   evaluates them or parses them as markup (text goes through textContent and
   confirm() only): an attribute value is data, not code, which is what closes the old defect
   class where a quote in an interpolated confirm() string broke the handler.

   The theme toggle is not here: it is on the login and consent pages too,
   which do not load this file, so its listener lives in `_theme.html`. */
(function () {
    'use strict';

    function closest(event, selector) {
        var t = event.target;
        if (!t || t.nodeType !== 1) { t = t && t.parentElement; }
        return t && t.closest ? t.closest(selector) : null;
    }

    /* Two modal styles exist: `.modal` dialogs (keys, users) shown by the
       `open` class, and the settings reset scrim, whose display is driven
       directly. CSSOM writes are not governed by CSP. */
    function showModal(el) {
        if (el.classList.contains('modal')) { el.classList.add('open'); }
        else { el.style.display = 'flex'; }
    }
    function hideModal(el) {
        if (el.classList.contains('modal')) { el.classList.remove('open'); }
        else { el.style.display = 'none'; }
    }

    function sidebarParts() {
        return [document.querySelector('.sidebar'), document.querySelector('.sidebar-backdrop')];
    }

    /* Edit-limit modal: one dialog reused for every key row, its form's
       action rewritten to the key being edited. The id is a database
       integer; anything else is ignored rather than put into a URL. */
    function editLimit(keyId, current) {
        if (!/^[0-9]+$/.test(keyId)) { return; }
        var form = document.getElementById('limit-form');
        form.action = '/admin/keys/' + keyId + '/limit';
        document.getElementById('limit-input').value = current;
        document.getElementById('limit-modal').classList.add('open');
    }

    /* Dashboard "Reindex Now": POST in the background with the CSRF token as
       a header, and report the result inline next to the button. */
    async function triggerReindex(form) {
        const btn = form.querySelector('button');
        const label = btn.querySelector('.btn-label');
        const icon  = btn.querySelector('.btn-icon');
        const status = document.getElementById('reindex-status');
        if (btn.disabled) return;

        btn.disabled = true;
        icon.classList.add('spin');
        const originalLabel = label.textContent;
        label.textContent = 'Starting…';
        status.classList.remove('show', 'err');
        status.classList.add('ok');
        status.textContent = '';

        try {
            const csrf = form.querySelector('input[name="csrf_token"]');
            const r = await fetch('/admin/settings/reindex', {
                method: 'POST',
                headers: {
                    'Accept': 'application/json',
                    ...(csrf ? { 'X-CSRF-Token': csrf.value } : {}),
                },
            });
            if (!r.ok) throw new Error('HTTP ' + r.status);
            status.textContent = '✓ Reindex started — new notes will appear shortly';
            status.classList.add('show');
            label.textContent = 'Started';
        } catch (e) {
            status.classList.remove('ok');
            status.classList.add('err', 'show');
            status.textContent = '✗ Failed to start reindex';
            label.textContent = 'Failed';
        }

        setTimeout(function() {
            icon.classList.remove('spin');
            label.textContent = originalLabel;
            btn.disabled = false;
        }, 2500);
        setTimeout(function() {
            status.classList.remove('show');
        }, 5000);
    }

    document.addEventListener('click', function (event) {
        var el;

        /* Destructive confirmations fail closed: the control is a
           type="button", so without this listener nothing is submitted. On
           acceptance the form is submitted through requestSubmit(), which
           runs the form's own validation and submit event. */
        el = closest(event, '[data-confirm]');
        if (el) {
            if (el.disabled || !el.form) { return; }
            if (window.confirm(el.getAttribute('data-confirm'))) {
                if (typeof el.form.requestSubmit === 'function') { el.form.requestSubmit(); }
                else { el.form.submit(); }
            }
            return;
        }

        el = closest(event, '[data-modal-open]');
        if (el) {
            var target = document.getElementById(el.getAttribute('data-modal-open'));
            if (target) { showModal(target); }
            return;
        }

        el = closest(event, '[data-modal-close]');
        if (el) {
            var modal = document.getElementById(el.getAttribute('data-modal-close'));
            if (modal) { hideModal(modal); }
            return;
        }

        /* Close only when the click lands on the scrim itself, not on
           anything inside the dialog. */
        el = event.target;
        if (el && el.nodeType === 1 && el.hasAttribute('data-modal-backdrop')) {
            hideModal(el);
            return;
        }

        el = closest(event, '[data-copy-from]');
        if (el) {
            var btn = el;
            var src = document.getElementById(btn.getAttribute('data-copy-from'));
            if (!src) { return; }
            navigator.clipboard.writeText(src.textContent).then(function () {
                btn.textContent = 'Copied!';
                setTimeout(function () { btn.textContent = 'Copy'; }, 2000);
            });
            return;
        }

        el = closest(event, '[data-limit-edit]');
        if (el) {
            editLimit(el.getAttribute('data-key-id') || '', el.getAttribute('data-limit') || '');
            return;
        }

        el = closest(event, '[data-sidebar-open]');
        if (el) {
            sidebarParts().forEach(function (p) { if (p) { p.classList.add('open'); } });
            return;
        }

        el = closest(event, '[data-sidebar-close]');
        if (el) {
            sidebarParts().forEach(function (p) { if (p) { p.classList.remove('open'); } });
            return;
        }
    });

    document.addEventListener('change', function (event) {
        var el = closest(event, '[data-autosubmit]');
        if (el && el.form) {
            if (typeof el.form.requestSubmit === 'function') { el.form.requestSubmit(); }
            else { el.form.submit(); }
        }
    });

    document.addEventListener('submit', function (event) {
        var form = event.target;
        if (form && form.nodeType === 1 && form.hasAttribute('data-async-reindex')) {
            event.preventDefault();
            triggerReindex(form);
        }
    });
})();
