const CACHE_NAME = 'farmaflush-v2';
const STATIC_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/img/icon-192.png',
  '/static/img/icon-512.png',
  '/img/logo.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') {
    return;
  }

  const request = event.request;
  const url = new URL(request.url);
  const isHxRequest = request.headers.get('HX-Request') === 'true';

  // Solo manejar peticiones same-origin
  if (url.origin !== self.location.origin) {
    return;
  }

  // HTMX devuelve fragmentos HTML: no mezclar estos responses con el cache
  // de páginas completas para evitar UI rota en navegación offline.
  if (isHxRequest) {
    event.respondWith(networkOnly(request));
    return;
  }

  // Navegación y endpoints dinámicos: Network First
  if (
    request.mode === 'navigate' ||
    url.pathname.startsWith('/buscar') ||
    url.pathname.startsWith('/medicamento/') ||
    url.pathname.startsWith('/verificar-precio') ||
    url.pathname.startsWith('/verificar-ticket') ||
    url.pathname.startsWith('/parafarmacia')
  ) {
    event.respondWith(networkFirst(request));
    return;
  }

  // Assets estáticos: Cache First
  if (url.pathname.startsWith('/static/') || url.pathname.startsWith('/img/')) {
    event.respondWith(cacheFirst(request));
    return;
  }

  // Resto: Network First
  event.respondWith(networkFirst(request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const response = await fetch(request);
    cache.put(request, response.clone());
    return response;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) {
      return cached;
    }
    const fallback = await cache.match('/');
    return (
      fallback ||
      new Response('Sin conexion y sin cache disponible.', {
        status: 503,
        headers: { 'Content-Type': 'text/plain; charset=utf-8' }
      })
    );
  }
}

async function networkOnly(request) {
  try {
    return await fetch(request);
  } catch (err) {
    return new Response('Sin conexion para cargar fragmento HTMX.', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' }
    });
  }
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  if (cached) {
    return cached;
  }
  const response = await fetch(request);
  cache.put(request, response.clone());
  return response;
}
