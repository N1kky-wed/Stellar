const CACHE_NAME = 'stellar-static-v8';
const ASSETS_TO_CACHE = [
  '/',
  '/default.min.css',
  '/custom_select.css',
  '/custom_select.js',
  '/highlight.min.js',
  '/marked.min.js',
  '/turndown.js',
  '/static/manifest.json',
  '/static/icon.svg',
  '/static/notification-icon.png'
];

// Install Event - Pre-cache minimal assets for offline verification
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[Service Worker] Pre-caching static app shell');
      return cache.addAll(ASSETS_TO_CACHE);
    }).then(() => self.skipWaiting())
  );
});

// Activate Event - Clean up stale caches
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(name => {
          if (name !== CACHE_NAME) {
            console.log('[Service Worker] Removing old cache:', name);
            return caches.delete(name);
          }
        })
      );
    }).then(() => self.clients.claim())
  );
});

// Fetch Event - Handle resource requests
self.addEventListener('fetch', event => {
  const requestUrl = new URL(event.request.url);

  // CRITICAL: Always use network-only for API calls, SSE streams, waitlist, login, and Google OAuth
  if (
    requestUrl.pathname.startsWith('/api/') ||
    requestUrl.pathname.startsWith('/login') ||
    requestUrl.pathname.includes('stream') ||
    requestUrl.pathname === '/upload_files' ||
    event.request.method !== 'GET'
  ) {
    event.respondWith(fetch(event.request));
    return;
  }

  // Stale-While-Revalidate for static visual assets and library JS/CSS
  event.respondWith(
    caches.match(event.request).then(cachedResponse => {
      if (cachedResponse) {
        // Trigger background fetch to refresh cache
        fetch(event.request).then(networkResponse => {
          if (networkResponse.status === 200) {
            caches.open(CACHE_NAME).then(cache => cache.put(event.request, networkResponse));
          }
        }).catch(() => {/* Ignore network failures in background */});
        
        return cachedResponse;
      }

      // Network Fallback
      return fetch(event.request).then(response => {
        // Cache valid static responses dynamically
        if (
          response.status === 200 &&
          (requestUrl.pathname.startsWith('/static/') || requestUrl.pathname.endsWith('.css') || requestUrl.pathname.endsWith('.js'))
        ) {
          const responseClone = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(event.request, responseClone));
        }
        return response;
      }).catch(err => {
        // Fallback for offline root page
        if (event.request.mode === 'navigate') {
          return caches.match('/');
        }
        throw err;
      });
    })
  );
});

// Push Event - Handle incoming background notifications
self.addEventListener('push', event => {
  let data = { title: 'Stellar', body: 'New agent notification' };
  try {
    if (event.data) {
      data = event.data.json();
    }
  } catch (e) {
    data = { title: 'Stellar', body: event.data.text() };
  }

  const options = {
    body: data.body,
    icon: '/static/icon.svg',
    badge: '/static/notification-icon.png',
    vibrate: [200, 100, 200],
    data: {
      url: data.url || '/'
    }
  };

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      const hasFocusedClient = clientList.some(client => client.focused);
      if (hasFocusedClient) {
        console.log('[Service Worker] App is focused. Skipping push notification.');
        return;
      }
      return self.registration.showNotification(data.title, options);
    })
  );
});

// Notification Click Event - Focus or launch the app shell
self.addEventListener('notificationclick', event => {
  event.notification.close();
  // Resolve targetUrl as a fully qualified absolute URL so that the WebAPK/Android intent filters launch the standalone app window
  const targetUrl = new URL(event.notification.data?.url || '/', self.location.origin).href;

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clientList => {
      // If a window is already open, navigate and focus it
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          return client.focus().then(() => {
            if (client.url !== targetUrl && 'navigate' in client) {
              return client.navigate(targetUrl);
            }
          });
        }
      }
      // If no window is open, open a new one
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
    })
  );
});
