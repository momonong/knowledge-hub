(function (destination, expectedOrigin) {
    if (location.origin !== expectedOrigin ||
        !['/knowledge/', '/knowledge/reading', '/knowledge/library', '/knowledge/manage'].some(function (path) {
            return destination === expectedOrigin + path;
        }) ||
        !(location.pathname === '/knowledge' || location.pathname.startsWith('/knowledge/'))) {
        return false;
    }
    if (location.href.split('#')[0] === destination) return true;

    const links = document.querySelectorAll('header.app-header nav a[href], header.app-header a.utility[href], header.topbar nav a[href]');
    const link = Array.from(links).find(function (anchor) {
        return anchor.href === destination && (!anchor.target || anchor.target === '_self') &&
            !anchor.hasAttribute('download');
    });
    if (link) {
        link.click();
        return true;
    }

    // A reader or a page with a pending operation must leave through its own link handler.
    if (document.querySelector('[data-reader], [data-draft], [data-busy]')) return false;
    const legacy = document.querySelector('header.topbar + main.container');
    const login = document.querySelector('body[data-mode="login"] #login-panel:not([hidden])');
    const idleLogin = login && login.querySelector('#login-form button[type="submit"]:enabled') &&
        !login.querySelector('#passkey-login:not([hidden]):disabled');
    if (!legacy && !idleLogin) return false;
    location.assign(destination);
    return true;
})
