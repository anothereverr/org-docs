/**
 * Inject a "Print / Save as PDF" button below the H1 heading of every page.
 * Clicking the button opens the browser's native print dialog; the user can
 * choose "Save as PDF" from there.  Print-specific styles live in custom.css.
 */
document.addEventListener("DOMContentLoaded", function () {
  var article = document.querySelector("article.md-content__inner");
  if (!article) return;

  var btn = document.createElement("button");
  btn.className = "pdf-download-btn";
  btn.setAttribute("title", "Print / Save as PDF");
  btn.innerHTML =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="14" height="14" '
    + 'fill="currentColor" aria-hidden="true">'
    + '<path d="M19 8H5c-1.66 0-3 1.34-3 3v6h4v4h12v-4h4v-6c0-1.66-1.34-3-3-3z'
    + 'm-3 11H8v-5h8v5zm3-7c-.55 0-1-.45-1-1s.45-1 1-1 1 .45 1 1-.45 1-1 1z'
    + 'm-1-9H6v4h12V3z"/>'
    + "</svg>"
    + " Print / Save as PDF";

  btn.addEventListener("click", function () {
    window.print();
  });

  var h1 = article.querySelector("h1");
  if (h1 && h1.parentNode) {
    h1.parentNode.insertBefore(btn, h1.nextSibling);
  } else {
    article.insertBefore(btn, article.firstChild);
  }
});
