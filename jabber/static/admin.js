// Admin "People" section in Settings, and the forced password change screen.
(function () {
  const $ = (id) => document.getElementById(id);
  async function api(path, body) {
    const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const r = await fetch(path, opt);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    return d;
  }
  function note(text, secret) {
    const el = $('peopleNote');
    el.textContent = '';
    if (!secret) { el.textContent = text; return; }
    el.append(text + ' ');
    const c = document.createElement('code');
    c.textContent = secret;
    el.append(c, ' (shown once; give it to them privately)');
  }
  function when(ts) {
    return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : 'never signed in';
  }
  async function renderPeople() {
    const box = $('peopleList');
    let d;
    try { d = await api('/api/admin/users'); } catch (e) { box.textContent = e.message; return; }
    box.textContent = '';
    for (const u of d.users) {
      const row = document.createElement('div');
      row.className = 'people-row';
      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = u.username + (u.name && u.name !== u.username ? ' \u00b7 ' + u.name : '') + ' \u00b7 ' + when(u.last_login);
      row.appendChild(who);
      for (const t of [u.role === 'admin' ? 'admin' : null, u.disabled ? 'disabled' : null, u.must_change ? 'has temp password' : null, u.you ? 'you' : null]) {
        if (!t) continue;
        const s = document.createElement('span'); s.className = 'tag'; s.textContent = t; row.appendChild(s);
      }
      const act = (label, fn) => {
        const b = document.createElement('button');
        b.textContent = label;
        b.addEventListener('click', async () => { try { await fn(); } catch (e) { note('Error: ' + e.message); } });
        row.appendChild(b);
      };
      act('Reset password', async () => {
        if (!confirm('Reset the password for ' + u.username + '? They are signed out everywhere.')) return;
        const r = await api('/api/admin/users/' + u.username + '/reset', {});
        note('Temporary password for ' + u.username + ':', r.temp_password); renderPeople();
      });
      if (!u.you) {
        act(u.disabled ? 'Enable' : 'Disable', async () => {
          await api('/api/admin/users/' + u.username + '/' + (u.disabled ? 'enable' : 'disable'), {}); renderPeople();
        });
        act(u.role === 'admin' ? 'Make regular' : 'Make admin', async () => {
          await api('/api/admin/users/' + u.username + '/role', { role: u.role === 'admin' ? 'user' : 'admin' }); renderPeople();
        });
        act('Remove', async () => {
          if (!confirm('Remove the account ' + u.username + '? Their saved chats stay on disk but nobody can sign in to them.')) return;
          await api('/api/admin/users/' + u.username + '/delete', {}); note(u.username + ' removed.'); renderPeople();
        });
      }
      box.appendChild(row);
    }
  }
  function init() {
    const me = window.JABBER_ME;
    if (!me || !me.authenticated) return;
    if (me.must_change) {
      $('pwOverlay').hidden = false;
      $('pwForm').addEventListener('submit', async (e) => {
        e.preventDefault();
        const err = $('pwErr');
        if ($('pwNew').value !== $('pwNew2').value) { err.textContent = 'The two new passwords do not match.'; return; }
        try {
          await api('/api/password', { current: $('pwCurrent').value, new: $('pwNew').value });
          location.reload();
        } catch (ex) { err.textContent = ex.message; }
      });
      return;
    }
    if (me.role === 'admin' && me.provider === 'local') {
      $('peopleSection').hidden = false;
      renderPeople();
      $('peopleAdd').addEventListener('submit', async (e) => {
        e.preventDefault();
        try {
          const r = await api('/api/admin/users', { username: $('peopleUser').value, name: $('peopleName').value,
                                                    role: $('peopleIsAdmin').checked ? 'admin' : 'user' });
          note('Let in ' + r.username + '. Temporary password:', r.temp_password);
          $('peopleUser').value = ''; $('peopleName').value = ''; $('peopleIsAdmin').checked = false;
          renderPeople();
        } catch (ex) { note('Error: ' + ex.message); }
      });
    }
  }
  // app.js sets JABBER_ME asynchronously after /api/me; wait for it.
  let tries = 0;
  const t = setInterval(() => { if (window.JABBER_ME || ++tries > 50) { clearInterval(t); init(); } }, 100);
})();
