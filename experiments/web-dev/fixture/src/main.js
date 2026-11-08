// The form writes "Submitted: <value>" into the page; the run's check expects that line.
const form = document.getElementById("form");
const result = document.getElementById("result");
form.addEventListener("submit", (e) => {
  e.preventDefault();
  result.textContent = "Submitted: " + document.getElementById("name").value;
});
