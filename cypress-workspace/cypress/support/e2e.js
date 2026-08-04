// ***********************************************************
// This example support/e2e.js is processed and
// loaded automatically before your test files.
//
// This is a great place to put global configuration and
// behavior that modifies Cypress.
//
// You can change the location of this file or turn off
// automatically serving support files with the
// 'supportFile' configuration option.
//
// You can read more here:
// https://on.cypress.io/configuration
// ***********************************************************

// Import commands.js using ES2015 syntax:
import './commands'
import 'cypress-real-events/support';
import 'cypress-iframe';

// Write test result log after each test
afterEach(function () {
    const status = this.currentTest?.state ?? 'unknown';
    const testName = this.currentTest?.title ?? 'unknown';
    cy.task('writeLog', `Test: ${testName} | Status: ${status}`);
});


Cypress.on('uncaught:exception', (err, runnable) => {
    // Only suppress known harmless framework/browser errors
    const ignoredMessages = [
        'ResizeObserver loop limit exceeded',
        'Non-Error promise rejection captured',
        'Script error.',
        'Cannot read properties of null',
    ];
    if (ignoredMessages.some(msg => err.message && err.message.includes(msg))) {
        return false;
    }
    // All other real application errors will fail the test
    return true;
});
  
 