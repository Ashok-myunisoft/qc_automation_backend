// cypress/support/commands.js
// Add custom Cypress commands here.
// These are available in every step-definition script.

Cypress.Commands.add("getByDataCy", (selector) => {
  return cy.get(`[data-cy="${selector}"]`);
});