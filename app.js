import { API_URL } from "./config.js";

const gallery = document.getElementById("gallery");
const status = document.getElementById("status");
const refresh = document.getElementById("refresh");
const objectUrls = new Set();
let controller;

function clearGallery() {
  // Cancel a previous request and release local image bytes on refresh or exit.
  controller?.abort();
  for (const url of objectUrls) URL.revokeObjectURL(url);
  objectUrls.clear();
  gallery.replaceChildren();
}

async function requestApi(path, signal) {
  let response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      signal, credentials: "omit", cache: "no-store",
    });
  } catch (error) {
    if (signal.aborted) throw error;
    throw new Error("Cannot reach the gallery API. Check the URL, deployment, and API CORS.");
  }
  if (!response.ok) throw new Error(`Gallery API returned HTTP ${response.status}.`);
  try {
    return await response.json();
  } catch {
    throw new Error("The gallery API returned invalid JSON.");
  }
}

async function loadImage(event, row, signal) {
  const message = document.createElement("p");
  message.textContent = "Loading image...";
  row.append(message);
  try {
    const data = await requestApi(`/events/${encodeURIComponent(event.eventId)}/image`, signal);
    let signedUrl;
    try {
      signedUrl = new URL(data.imageUrl);
    } catch {
      throw new Error("The API returned an invalid image URL.");
    }
    if (signedUrl.protocol !== "https:" || !signedUrl.hostname.endsWith(".amazonaws.com")) {
      throw new Error("The API returned an unexpected image address.");
    }
    let response;
    try {
      // No AWS keys, custom headers, or cookies: the signature grants this read.
      response = await fetch(signedUrl, {
        signal, credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
      });
    } catch (error) {
      if (signal.aborted) throw error;
      throw new Error("Cannot read the S3 image. Check the network and S3 CORS origin.");
    }
    if (!response.ok) throw new Error(`S3 returned HTTP ${response.status}. Refresh for a new link.`);
    const blob = await response.blob();
    if (!["image/png", "image/jpeg", "image/webp"].includes(blob.type)) {
      throw new Error("S3 did not return a supported image type.");
    }
    if (signal.aborted) return;
    const image = document.createElement("img");
    image.alt = event.imageAlt;
    const localUrl = URL.createObjectURL(blob);
    objectUrls.add(localUrl);
    image.src = localUrl;
    try {
      await image.decode();
    } catch {
      URL.revokeObjectURL(localUrl);
      objectUrls.delete(localUrl);
      throw new Error("The image bytes could not be displayed.");
    }
    if (signal.aborted) return;
    row.append(image);
    message.textContent = "Image loaded from private S3 storage.";
  } catch (error) {
    // Cancellation is intentional; all other errors remain visible on their card.
    if (!signal.aborted) message.textContent = error.message;
  }
}

async function loadGallery() {
  clearGallery();
  controller = new AbortController();
  const { signal } = controller;
  refresh.disabled = true;
  status.textContent = "Loading events...";
  try {
    const data = await requestApi("/events", signal);
    if (!Array.isArray(data.events) || data.events.some((event) =>
      !event || ["eventId", "title", "description", "imageAlt"].some((key) =>
        typeof event[key] !== "string" || !event[key].trim()))) {
      throw new Error("Unexpected event data from the gallery API.");
    }
    if (signal.aborted) return;
    if (data.events.length === 0) {
      status.textContent = "No events are available.";
      return;
    }
    const imageRequests = data.events.map((event) => {
      const row = document.createElement("li");
      const heading = document.createElement("h2");
      const description = document.createElement("p");
      // textContent treats API text as text, never executable markup.
      heading.textContent = event.title;
      description.textContent = event.description;
      row.append(heading, description);
      gallery.append(row);
      return loadImage(event, row, signal);
    });
    await Promise.all(imageRequests);
    if (!signal.aborted) {
      status.textContent = `${data.events.length} events listed. Check each card for image results.`;
    }
  } catch (error) {
    if (!signal.aborted) status.textContent = error.message;
  } finally {
    if (!signal.aborted) refresh.disabled = false;
  }
}

refresh.addEventListener("click", loadGallery);
window.addEventListener("pagehide", clearGallery);
// Restore the gallery if the browser returns to this page from its back/forward cache.
window.addEventListener("pageshow", (event) => { if (event.persisted) loadGallery(); });
loadGallery();