'use strict';
(() => {
  const {mode, prefix} = document.body.dataset;
  const byId = id => document.getElementById(id);
  const result = byId('result');
  const manageThisDevice = location.pathname === prefix + '/access/login' &&
    new URLSearchParams(location.search).get('trust') === '1';
  let csrf = '', ready = false, busy = false;

  const messages = {
    invalid_login: '密碼不對，請再確認。',
    invalid_password: '請輸入 12–128 字的密碼，不可含換行。',
    rate_limited: '嘗試次數較多，請五分鐘後再試。',
    not_configured: '尚未設定 KH 密碼，請先由站主設定。',
    owner_required: '站主管理登入已過期，請重新登入。',
    csrf_rejected: '登入狀態已過期，請重新整理後再試。',
    owner_unavailable: '首頁管理服務暫時無法連線，請稍後再試。',
    access_unavailable: 'KH 登入服務暫時無法連線，請稍後再試。',
    passkeys_unavailable: '目前無法使用通行金鑰，可以用 KH 密碼登入。',
    recent_password_required: '請重新輸入 KH 密碼，再綁定這支裝置。',
    invalid_device_name: '請為裝置取一個容易辨認的名稱。',
    invalid_passkey_challenge: '這次通行金鑰確認已過期，請再試一次。',
    invalid_passkey: '這把通行金鑰無法登入。可換一把金鑰，或用 KH 密碼登入。',
    credential_already_enrolled: '這把通行金鑰已綁定，可從登入頁使用。',
  };
  function message(text, success = false) {
    result.textContent = text;
    result.toggleAttribute('data-success', success);
  }
  async function api(path, value) {
    const init = {credentials: 'same-origin', cache: 'no-store', redirect: 'error'};
    if (value !== undefined) {
      Object.assign(init, {method: 'POST', headers: {'Content-Type':'application/json', 'X-CSRF-Token':csrf},
        body: JSON.stringify(value)});
    }
    let response;
    try { response = await fetch(path, init); } catch {
      throw new Error(value === undefined ? '無法確認登入，請重新整理後再試。' :
        '未收到操作結果，請先重新整理，核對登入或裝置狀態。');
    }
    let data;
    try { data = await response.json(); } catch {
      throw new Error('無法確認操作結果，請重新整理並核對狀態。');
    }
    if (!response.ok) {
      const fallback = path.includes('/passkey/') ? '這次通行金鑰驗證未完成。可再試一次，或改用 KH 密碼。' :
        '這次沒有完成，請重新整理後再試。';
      const error = new Error(messages[data.error] || fallback);
      error.code = data.error;
      throw error;
    }
    return data;
  }
  async function act(button, handler) {
    if (!ready || busy) return;
    busy = true;
    button.disabled = true;
    message('');
    try {
      await handler();
    } catch (err) {
      if (err.code === 'recent_password_required' || (err.code === 'csrf_rejected' && !byId('enroll-panel').hidden)) {
        byId('enroll-panel').hidden = true;
        byId('login-panel').hidden = false;
        try {
          const session = await api(prefix+'/access/session');
          csrf = session.csrf_token;
          byId('login-password').focus();
          message(messages.recent_password_required);
        } catch (refreshError) { message(refreshError.message); }
      } else if (err.name === 'NotAllowedError' || err.name === 'AbortError') {
        message('尚未完成裝置驗證，你可以再試一次或用 KH 密碼登入。');
      } else if (err.name === 'InvalidStateError') {
        message('裝置已有通行金鑰，可返回登入頁使用，或在設定中管理。');
      } else {
        message(err.message || '操作未完成，請再試一次。');
      }
    } finally {
      busy = false;
      button.disabled = false;
    }
  }
  function form(id, handler) {
    byId(id).addEventListener('submit', event => {
      event.preventDefault();
      act(event.currentTarget.querySelector('button[type=submit]'), handler);
    });
  }
  const click = (id, handler) => byId(id).addEventListener('click', event => act(event.currentTarget, handler));
  const dateText = seconds => new Intl.DateTimeFormat('zh-TW', {dateStyle:'medium'}).format(new Date(seconds * 1000));

  function returnToReading() {
    if (location.pathname.startsWith(prefix + '/access/')) location.replace(prefix + '/');
    else location.reload(); // A challenge rendered at an article URL retains its version and fragment.
  }
  function offerEnrollment() {
    byId('login-panel').hidden = true;
    byId('enroll-panel').hidden = false;
    byId('enroll-panel').querySelector('h1').focus();
  }
  async function platformCanVerify() {
    if (!window.isSecureContext || !window.PublicKeyCredential || !navigator.credentials) return false;
    try { return await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable(); }
    catch { return false; }
  }
  // Keep earlier WebViews usable when they implement WebAuthn but not the JSON convenience methods.
  function bufferFromBase64(value) {
    return Uint8Array.from(atob(value.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  }
  function base64FromBuffer(buffer) {
    if (buffer == null) return null;
    return btoa(Array.from(new Uint8Array(buffer), n => String.fromCharCode(n)).join(''))
      .replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
  }
  function publicOptions(json, register) {
    const parse = register ? PublicKeyCredential.parseCreationOptionsFromJSON : PublicKeyCredential.parseRequestOptionsFromJSON;
    if (typeof parse === 'function') return parse.call(PublicKeyCredential, json);
    const opts = {...json, challenge:bufferFromBase64(json.challenge)};
    if (register) opts.user = {...json.user, id:bufferFromBase64(json.user.id)};
    for (const key of ['excludeCredentials', 'allowCredentials']) {
      if (json[key]) opts[key] = json[key].map(cred => ({...cred, id:bufferFromBase64(cred.id)}));
    }
    return opts;
  }
  function credentialJSON(credential, register) {
    if (typeof credential.toJSON === 'function') return credential.toJSON();
    const answer = credential.response;
    const response = {clientDataJSON:base64FromBuffer(answer.clientDataJSON)};
    if (register) {
      response.attestationObject = base64FromBuffer(answer.attestationObject);
    } else {
      response.authenticatorData = base64FromBuffer(answer.authenticatorData);
      response.signature = base64FromBuffer(answer.signature);
      response.userHandle = base64FromBuffer(answer.userHandle);
    }
    return {id:credential.id, rawId:base64FromBuffer(credential.rawId), type:credential.type,
      response, clientExtensionResults:credential.getClientExtensionResults()};
  }
  async function usePasskey(register) {
    const route = prefix + '/access/passkey/' + (register ? 'register' : 'login');
    const options = await api(route + '/begin', register ? {name:byId('device-name').value.trim()} : {});
    const request = {publicKey:publicOptions(options.publicKey, register)};
    const credential = register ? await navigator.credentials.create(request) : await navigator.credentials.get(request);
    if (!credential) throw new Error('未選取通行金鑰。');
    await api(route + '/finish', {credential:credentialJSON(credential, register)});
    returnToReading();
  }

  function smallButton(label) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = label;
    return button;
  }
  function deviceRow(device) {
    const row = document.createElement('li');
    const title = document.createElement('strong');
    title.textContent = device.name;
    const lastUsed = document.createElement('p');
    lastUsed.className = 'hint';
    lastUsed.textContent = '最近驗證於 ' + dateText(device.last_used_at || device.created_at);
    const revoke = smallButton('撤銷');
    row.append(title, lastUsed, revoke);
    revoke.addEventListener('click', () => {
      const prompt = document.createElement('p');
      prompt.textContent = '撤銷後，這支裝置將登出，這把通行金鑰也會失效。';
      const confirm = smallButton('確認撤銷'), cancel = smallButton('取消');
      cancel.addEventListener('click', () => { row.replaceWith(deviceRow(device)); });
      confirm.addEventListener('click', () => act(confirm, async () => {
        await api(prefix + '/access/devices/revoke', {id:device.id});
        row.remove();
        message('已撤銷「' + device.name + '」。', true);
        await refreshDeviceList();
      }));
      revoke.replaceWith(prompt, confirm, cancel);
      cancel.focus();
    });
    return row;
  }
  async function listDevices() {
    const {devices} = await api(prefix + '/access/devices/list', {});
    const active = devices.filter(device => !device.revoked_at);
    byId('trusted-device-list').replaceChildren(...active.map(deviceRow));
    byId('device-list-status').textContent = active.length ? '' : '目前沒有已綁定的裝置。';
  }
  async function refreshDeviceList() {
    try { await listDevices(); }
    catch (err) { byId('device-list-status').textContent = err.message; }
  }
  async function initialize() {
    ready = false;
    try {
      const settings = mode === 'settings';
      const session = await api(settings ? '/home-api/session' : prefix+'/access/session');
      csrf = session.csrf_token;
      if (settings) {
        byId('owner-form').hidden = session.authenticated;
        for (const id of ['settings-form','owner-logout','trusted-devices']) byId(id).hidden = !session.authenticated;
        byId('trusted-device-list').replaceChildren();
        message(session.authenticated ? '' : '請先用首頁站主管理密碼登入。');
        if (session.authenticated) await refreshDeviceList();
      } else if (mode === 'logout') {
        byId('reader-logout').disabled = !session.authenticated;
        if (!session.authenticated) message('目前已登出 Knowledge Hub。', true);
      } else {
        const login = byId('login-form').querySelector('button[type=submit]');
        login.disabled = !session.configured;
        if (session.authenticated && !manageThisDevice) { returnToReading(); return; }
        if (manageThisDevice && session.trusted_device) {
          byId('login-panel').hidden = true;
          byId('trusted-panel').hidden = false;
          byId('trust-summary').textContent = session.trusted_device.name + '可直接續讀至 ' + dateText(session.trusted_until) + '。';
        } else {
          const available = session.configured && session.passkeys_available && await platformCanVerify();
          byId('trust-choice').hidden = !available;
          byId('passkey-login').hidden = !available;
          byId('trust-device').checked = Boolean(manageThisDevice && available);
          if (manageThisDevice && available && session.can_register_passkey) offerEnrollment();
          else if (!session.configured) message(messages.not_configured);
          else if (manageThisDevice) message(available ? '請輸入 KH 密碼，確認要綁定這支裝置。' :
            '這個瀏覽器或裝置目前無法建立通行金鑰，可以繼續使用 KH 密碼。');
        }
      }
      ready = true;
    } catch (err) { message(err.message); }
  }
  byId((mode === 'settings' ? 'settings' : mode === 'logout' ? 'logout' : 'login')+'-panel').hidden = false;
  form('login-form', async () => {
    const session = await api(prefix+'/access/login', {password:byId('login-password').value});
    byId('login-password').value = '';
    csrf = session.csrf_token;
    if (byId('trust-device').checked && !byId('trust-choice').hidden && session.can_register_passkey) offerEnrollment();
    else returnToReading();
  });
  form('enroll-form', () => usePasskey(true));
  click('passkey-login', () => usePasskey(false));
  click('skip-enroll', returnToReading);
  form('owner-form', async () => {
    await api('/home-api/login', {password:byId('owner-password').value});
    byId('owner-password').value = '';
    await initialize();
  });
  form('settings-form', async () => {
    if (byId('new-password').value !== byId('repeat-password').value) throw new Error('兩次密碼不一樣，請再確認。');
    await api(prefix+'/access/password', {password:byId('new-password').value});
    byId('new-password').value = byId('repeat-password').value = '';
    message('KH 密碼已更新，所有裝置已登出。', true);
    await refreshDeviceList();
  });
  click('owner-logout', async () => { await api('/home-api/logout',{}); await initialize(); });
  click('reader-logout', async () => { await api(prefix+'/access/logout',{}); location.replace(prefix+'/'); });
  initialize();
})();
