(() => {
  const filters = document.querySelectorAll("[data-days-filter]");
  const plans = document.querySelectorAll("[data-plan-days]");

  if (!filters.length || !plans.length) return;

  function applyDaysFilter(activeFilter) {
    const selectedDays = activeFilter.dataset.daysFilter;

    filters.forEach((button) => {
      const isSelected = button === activeFilter;
      button.classList.toggle("is-selected", isSelected);
      button.setAttribute("aria-pressed", String(isSelected));
    });

    plans.forEach((plan) => {
      const shouldHide = selectedDays !== "all"
        && plan.dataset.planDays !== selectedDays;
      plan.classList.toggle("is-filtered-out", shouldHide);
      plan.querySelector('input[name="plan_key"]').disabled = shouldHide;
    });
  }

  filters.forEach((button) => {
    button.addEventListener("click", () => applyDaysFilter(button));
  });
})();
