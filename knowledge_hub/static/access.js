'use strict';
(() => {
  const mode=document.body.dataset.mode, prefix=document.body.dataset.prefix;
  const byId=id=>document.getElementById(id), result=byId('result');
  let csrf='', ready=false;
  const messages={invalid_login:'密碼不對，請再確認。',invalid_password:'請輸入 12–128 字的密碼，不可含換行。',
    rate_limited:'嘗試次數較多，請五分鐘後再試。',not_configured:'尚未設定 KH 密碼，請先由站主設定。',
    owner_required:'管理登入已過期，請重新整理後登入站主管理。',csrf_rejected:'登入狀態已過期，請重新整理後再試。',
    owner_unavailable:'首頁管理服務暫時無法連線，請稍後再試。',access_unavailable:'KH 登入服務暫時無法連線，請稍後再試。'};
  function message(text,success=false){result.textContent=text;result.toggleAttribute('data-success',success);}
  async function api(path,value){
    let response;
    try{response=await fetch(path,{credentials:'same-origin',cache:'no-store',redirect:'error',
      ...(value===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(value)})});}
    catch{throw new Error(value===undefined?'無法確認登入，請重新整理後再試。':'未收到操作結果，請勿重複送出。請重新開啟 KH 核對登入或密碼是否已生效。');}
    let data;try{data=await response.json();}catch{throw new Error('未能確認操作結果，請重新開啟 KH 核對；不要直接重送。');}
    if(!response.ok)throw new Error(messages[data.error]||'這次沒有完成，請重新整理後再試。');
    return data;
  }
  function form(id,action){byId(id).addEventListener('submit',async event=>{
    event.preventDefault();if(!ready)return;
    const button=event.currentTarget.querySelector('button'), label=button.textContent;
    button.disabled=true;button.textContent='處理中…';message('');
    try{await action();}catch(error){message(error.message);}finally{button.disabled=false;button.textContent=label;}
  });}
  async function initialize(){
    ready=false;
    try{
      const session=await api(mode==='settings'?'/home-api/session':prefix+'/access/session');csrf=session.csrf_token;
      if(mode==='settings'){
        byId('owner-form').hidden=session.authenticated;byId('settings-form').hidden=!session.authenticated;
        byId('owner-logout').hidden=!session.authenticated;
        message(session.authenticated?'已登入站主管理，可以自行設定或更換 KH 密碼。':'請先用首頁站主管理密碼登入，以設定 KH 專用密碼。',session.authenticated);
      }else if(mode==='logout'){
        byId('reader-logout').disabled=!session.authenticated;
        if(!session.authenticated)message('目前已登出 Knowledge Hub。',true);
      }else{
        byId('login-form').querySelector('button').disabled=!session.configured;
        if(session.authenticated){location.replace(prefix+'/');return;}
        message(session.configured?'':messages.not_configured);
      }
      ready=true;
    }catch(error){message(error.message);}
  }
  byId((mode==='settings'?'settings':mode==='logout'?'logout':'login')+'-panel').hidden=false;
  form('login-form',async()=>{await api(prefix+'/access/login',{password:byId('login-password').value});byId('login-password').value='';
    if(location.pathname.startsWith(prefix+'/access/'))location.assign(prefix+'/');else location.reload();});
  form('owner-form',async()=>{await api('/home-api/login',{password:byId('owner-password').value});byId('owner-password').value='';await initialize();});
  form('settings-form',async()=>{
    if(byId('new-password').value!==byId('repeat-password').value)throw new Error('兩次密碼不一樣，請再確認。');
    await api(prefix+'/access/password',{password:byId('new-password').value});
    byId('new-password').value='';byId('repeat-password').value='';message('KH 密碼已儲存。請前往 Knowledge Hub，使用這組新密碼登入。',true);
  });
  byId('owner-logout').addEventListener('click',async()=>{if(!ready)return;try{await api('/home-api/logout',{});await initialize();}catch(error){message(error.message);}});
  byId('reader-logout').addEventListener('click',async()=>{if(!ready)return;byId('reader-logout').disabled=true;
    try{await api(prefix+'/access/logout',{});location.replace(prefix+'/');}catch(error){message(error.message);}});
  initialize();
})();
