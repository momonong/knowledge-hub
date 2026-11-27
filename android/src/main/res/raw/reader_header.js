(function (expectedOrigin) {
    if (window.top !== window || location.origin !== expectedOrigin ||
        !(location.pathname === '/knowledge' || location.pathname.startsWith('/knowledge/'))) return;

    function useAppHeader() {
        const reader = document.querySelector('body > header.app-header');
        if (reader) {
            // Keep the real links and their draft-saving click handlers for native navigation.
            reader.hidden = true;
            return true;
        }
        const loginBrand = document.querySelector('body[data-mode="login"] > header > a.brand');
        if (loginBrand) {
            loginBrand.style.display = 'none';
            return true;
        }
        const library = document.querySelector('header.topbar .topbar-inner');
        const tools = library && library.querySelector('.library-tools > summary');
        const brand = library && library.querySelector('a.brand');
        if (brand && tools) {
            brand.style.display = 'none';
            tools.style.position = 'static';
            tools.style.width = 'max-content';
            return true;
        }
        return false;
    }

    if (useAppHeader() || document.readyState !== 'loading') return;
    const observer = new MutationObserver(function () {
        if (useAppHeader()) observer.disconnect();
    });
    observer.observe(document, {childList: true, subtree: true});
    document.addEventListener('DOMContentLoaded', function () { observer.disconnect(); }, {once: true});
})
