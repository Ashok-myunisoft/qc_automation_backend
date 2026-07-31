// cypress/pageObject/Common.js
// Shared page-object helpers used across all screen step-definitions.

export const clickSave   = () => cy.get('[data-cy="Save"]').click();
export const clickSubmit = () => cy.get('[data-cy="Submit"]').click();
export const clickDelete = () => cy.get('[data-cy="Delete"]').click();
export const clickUpdate = () => cy.get('[data-cy="Update"]').click();

export const fillField = (dataCy, value) =>
  cy.get(`[data-cy="${dataCy}"]`).clear().type(value);

export const selectPicklist = (dataCy, value) =>
  cy.get(`[data-cy="${dataCy}"]`).select(value);

export const assertValidationError = (dataCy) =>
  cy.get(`[data-cy="${dataCy}"]`).should("contain.text", "required");

export const assertPageTitle = (dataCy, text) =>
  cy.get(`[data-cy="${dataCy}"]`).should("contain.text", text);