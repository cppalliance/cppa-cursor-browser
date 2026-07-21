// Apply stored theme before the stylesheet loads to avoid a dark->light flash.
(function () {
  try {
    var t = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}
})();
