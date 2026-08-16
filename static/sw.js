"use strict";

const CACHE_NAME = "ohs-diriyah-v1";
const APP_SHELL = [
  "/",
  "/near-miss",
  "/violation",
  "/static/style.css",
  "/static/form.js",
  "/static/near_miss.js",
  "/static/violation.js",
  "/static/photo_upload.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  // Never cache admin pages (session-specific, contains records/PII) or non-GET requests.
  if (event.request.method !== "GET" || url.pathname.startsWith("/admin")) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
