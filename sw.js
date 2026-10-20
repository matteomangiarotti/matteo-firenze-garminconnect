const CACHE_NAME = 'matteo-firenze-v19';

// Install: skip waiting immediately
self.addEventListener('install', event => {
  self.skipWaiting();
});

// Activate: pulisci cache vecchie
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: network-first per tutto tranne le risorse esterne pesanti
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Font e librerie esterne: cache-first (cambiano raramente)
  if (url.hostname === 'fonts.googleapis.com' ||
      url.hostname === 'fonts.gstatic.com' ||
      url.hostname === 'cdn.jsdelivr.net' ||
      url.hostname === 'cdnjs.cloudflare.com') {
    event.respondWith(
      caches.match(event.request).then(cached => {
        if (cached) return cached;
        return fetch(event.request).then(response => {
          const clone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
          return response;
        });
      })
    );
    return;
  }

  // Tutto il resto (index.html, piano.ics, sw.js ecc): network-first
  event.respondWith(
    fetch(event.request).then(response => {
      // Aggiorna la cache con la versione fresca
      if (response && response.status === 200) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
      }
      return response;
    }).catch(() => {
      // Offline: usa la cache
      return caches.match(event.request);
    })
  );
});

// Messaggi
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
