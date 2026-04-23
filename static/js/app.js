function rafThrottle(fn) {
  var scheduled = false;
  return function throttled() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(function () {
      scheduled = false;
      fn();
    });
  };
}

document.addEventListener('DOMContentLoaded', function () {
  const pwaInstallBanner = document.getElementById('pwa-install-banner');
  const pwaInstallBtn = document.getElementById('pwa-install-btn');
  const pwaInstallClose = document.getElementById('pwa-install-close');
  const PWA_BANNER_SNOOZE_UNTIL_KEY = 'ff_pwa_install_banner_snooze_until_v1';
  const PWA_INSTALLED_KEY = 'ff_pwa_installed_v1';
  const PWA_BANNER_SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;
  let deferredInstallPrompt = null;

  const isIos = /iPad|iPhone|iPod/.test(window.navigator.userAgent) || (window.navigator.userAgent.includes('Mac') && 'ontouchend' in document);
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
  const isMobile = window.matchMedia('(pointer: coarse)').matches || /Android|iPhone|iPad|iPod|Mobile/i.test(window.navigator.userAgent);

  function showPwaBanner() {
    if (!pwaInstallBanner) return;
    pwaInstallBanner.hidden = false;
    pwaInstallBanner.classList.add('is-visible');
  }

  function hidePwaBanner() {
    if (!pwaInstallBanner) return;
    pwaInstallBanner.classList.remove('is-visible');
    pwaInstallBanner.hidden = true;
  }

  function isPwaInstalled() {
    try { return localStorage.getItem(PWA_INSTALLED_KEY) === '1'; } catch (e) { return false; }
  }

  function markPwaInstalled() {
    try {
      localStorage.setItem(PWA_INSTALLED_KEY, '1');
      localStorage.removeItem(PWA_BANNER_SNOOZE_UNTIL_KEY);
    } catch (e) {}
  }

  function snoozePwaBanner() {
    try {
      localStorage.setItem(PWA_BANNER_SNOOZE_UNTIL_KEY, String(Date.now() + PWA_BANNER_SNOOZE_MS));
    } catch (e) {}
  }

  function isPwaBannerSnoozed() {
    try {
      const raw = localStorage.getItem(PWA_BANNER_SNOOZE_UNTIL_KEY);
      const until = raw ? Number(raw) : 0;
      return Number.isFinite(until) && until > Date.now();
    } catch (e) {
      return false;
    }
  }

  function updateInstallButtonLabel() {
    if (!pwaInstallBtn) return;
    if (deferredInstallPrompt) {
      pwaInstallBtn.textContent = 'Instalar app';
      return;
    }
    pwaInstallBtn.textContent = isIos ? 'Como instalar' : 'Instalar app';
  }

  if (isMobile && !isStandalone && !isPwaInstalled() && !isPwaBannerSnoozed()) {
      updateInstallButtonLabel();
      showPwaBanner();
  }

  if (pwaInstallClose) {
    pwaInstallClose.addEventListener('click', function () {
      snoozePwaBanner();
      hidePwaBanner();
    });
  }

  if (pwaInstallBtn) {
    pwaInstallBtn.addEventListener('click', async function () {
      if (deferredInstallPrompt) {
        deferredInstallPrompt.prompt();
        const choice = await deferredInstallPrompt.userChoice;
        if (choice && choice.outcome === 'accepted') {
          markPwaInstalled();
          hidePwaBanner();
        } else {
          snoozePwaBanner();
          hidePwaBanner();
        }
        deferredInstallPrompt = null;
        updateInstallButtonLabel();
        return;
      }

      if (isIos) {
        window.alert('Para instalar en iPhone: abre esta pagina en Safari, pulsa Compartir y luego "Anadir a pantalla de inicio".');
        return;
      }

      window.alert('Para instalar: abre el menu del navegador y pulsa "Instalar aplicacion" o "Anadir a pantalla de inicio".');
    });
  }

  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    deferredInstallPrompt = event;
    updateInstallButtonLabel();

    if (isMobile && !isStandalone && !isPwaInstalled() && !isPwaBannerSnoozed()) {
      showPwaBanner();
    }
  });

  window.addEventListener('appinstalled', function () {
    markPwaInstalled();
    hidePwaBanner();
  });

  const isStandalonePWA = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

  if (isStandalonePWA) {
    document.body.classList.add('pwa-standalone');

    const markAppReady = () => {
      document.body.classList.add('app-ready');
    };

    window.addEventListener('load', markAppReady, { once: true });
    window.setTimeout(markAppReady, 1800);
  }

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js')
        .then(function (reg) { console.log('SW registrado', reg); })
        .catch(function (err) { console.log('Error registro SW', err); });
    });
  }

  const scrollButton = document.getElementById('scroll-top-btn');
  const progressBar = document.getElementById('global-progress');
  let shouldScrollToTopAfterSwap = false;

  const showProgressBar = () => {
    if (progressBar) {
      progressBar.classList.add('is-visible');
    }
  };

  const hideProgressBar = () => {
    if (progressBar) {
      progressBar.classList.remove('is-visible');
    }
  };

  if (!scrollButton) {
    return;
  }

  const syncScrollButton = () => {
    const shouldShow = window.scrollY > 160;
    scrollButton.classList.toggle('is-visible', shouldShow);
  };

  scrollButton.addEventListener('click', () => {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });

  document.body.addEventListener('htmx:beforeRequest', (event) => {
    const trigger = event.detail.elt;
    shouldScrollToTopAfterSwap = Boolean(
      trigger && trigger.closest('.paginacion-form')
    );
    showProgressBar();
  });

  document.body.addEventListener('htmx:afterSwap', (event) => {
    const target = event.detail.target;
    if (!shouldScrollToTopAfterSwap || !target || target.id !== 'resultados') {
      return;
    }

    shouldScrollToTopAfterSwap = false;
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  });

  document.body.addEventListener('htmx:afterRequest', hideProgressBar);
  document.body.addEventListener('htmx:responseError', hideProgressBar);
  document.body.addEventListener('htmx:sendError', hideProgressBar);

  window.addEventListener('scroll', rafThrottle(syncScrollButton), { passive: true });
  syncScrollButton();

  var UMBRAL = 80;
  var tieneHeroLogo = !!document.querySelector('.hero-logo');
  var estaCompacto = false;

  function actualizarLogo() {
    if (!tieneHeroLogo) {
      document.body.classList.add('logo-compacto');
      return;
    }
    var scrollY = window.scrollY || window.pageYOffset;
    if (scrollY > UMBRAL && !estaCompacto) {
      document.body.classList.add('logo-compacto');
      estaCompacto = true;
    } else if (scrollY <= UMBRAL && estaCompacto) {
      document.body.classList.remove('logo-compacto');
      estaCompacto = false;
    }
  }

  window.addEventListener('scroll', rafThrottle(actualizarLogo), { passive: true });
  actualizarLogo();

  var STORAGE_KEY = 'ff_favoritos';

  function leerFavoritos() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); } catch (e) { return []; }
  }

  function guardarFavoritos(favs) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(favs)); } catch (e) {}
  }

  function esFavorito(nregistro) {
    return leerFavoritos().some(function (f) { return f.nregistro === nregistro; });
  }

  function toggleFavorito(nregistro) {
    var favs = leerFavoritos();
    var idx = favs.findIndex(function (f) { return f.nregistro === nregistro; });
    if (idx >= 0) {
      favs.splice(idx, 1);
      guardarFavoritos(favs);
      return false;
    }

    favs.push({ nregistro: nregistro, guardado: Date.now() });
    guardarFavoritos(favs);
    try { fetch('/medicamento/' + encodeURIComponent(nregistro), { method: 'GET', credentials: 'same-origin' }); } catch (e) {}
    return true;
  }

  function actualizarBoton(btn, esFav) {
    btn.textContent = esFav ? '★' : '☆';
    btn.title = esFav ? 'Quitar de favoritos' : 'Guardar en favoritos';
    btn.classList.toggle('es-favorito', esFav);
    btn.setAttribute('aria-pressed', esFav ? 'true' : 'false');
  }

  function actualizarBadge() {
    var badge = document.getElementById('fav-count-badge');
    if (!badge) return;
    var n = leerFavoritos().length;
    badge.textContent = n > 0 ? n : '';
    badge.style.display = n > 0 ? 'inline-flex' : 'none';
  }

  function inicializarBotones() {
    document.querySelectorAll('.btn-fav[data-nregistro]').forEach(function (btn) {
      actualizarBoton(btn, esFavorito(btn.dataset.nregistro));
    });
    actualizarBadge();
  }

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.btn-fav[data-nregistro]');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    var nr = btn.dataset.nregistro;
    var ahora = toggleFavorito(nr);
    document.querySelectorAll('.btn-fav[data-nregistro="' + nr + '"]').forEach(function (b) {
      actualizarBoton(b, ahora);
    });
    actualizarBadge();
  });

  document.body.addEventListener('htmx:afterSwap', function () {
    inicializarBotones();
  });

  inicializarBotones();

  window.FF_Favoritos = {
    leer: leerFavoritos,
    borrar: function (nr) {
      var favs = leerFavoritos().filter(function (f) { return f.nregistro !== nr; });
      guardarFavoritos(favs);
      actualizarBadge();
    },
    limpiar: function () {
      guardarFavoritos([]);
      actualizarBadge();
    }
  };
});
