const CACHE_NAME = 'garmincoach-cache-v3';
const URLS_TO_CACHE = [
  '/static/style.css',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  'https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=Inter:wght@400;500;600&display=swap'
];

// Install event: cache assets
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => {
        return cache.addAll(URLS_TO_CACHE);
      })
  );
  self.skipWaiting();
});

// Activate event: clean up old caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          if (cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  self.clients.claim();
});

// Fetch event: serve from cache if available, else fetch from network
self.addEventListener('fetch', event => {
  // Dynamic pages contain user-specific, rapidly changing state. Always let
  // navigations reach the app instead of returning stale cached HTML.
  if (event.request.method !== 'GET') return;
  if (event.request.mode === 'navigate') return;

  const url = new URL(event.request.url);
  const isLocalStaticAsset = url.origin === self.location.origin
    && url.pathname.startsWith('/static/');
  const isCachedFont = url.hostname === 'fonts.googleapis.com';
  if (!isLocalStaticAsset && !isCachedFont) return;
  
  event.respondWith(
    caches.match(event.request)
      .then(response => {
        // Cache hit - return response
        if (response) {
          // Fetch from network in background to update cache
          fetch(event.request).then(netResponse => {
            if (netResponse && netResponse.status === 200) {
              caches.open(CACHE_NAME).then(cache => {
                cache.put(event.request, netResponse.clone());
              });
            }
          }).catch(() => {});
          
          return response;
        }

        // Network fallback
        return fetch(event.request).then(response => {
          // Check if we received a valid response
          if(!response || response.status !== 200 || response.type !== 'basic') {
            return response;
          }

          // Clone the response because it's a stream
          var responseToCache = response.clone();
          caches.open(CACHE_NAME)
            .then(cache => {
              cache.put(event.request, responseToCache);
            });

          return response;
        });
      })
  );
});
