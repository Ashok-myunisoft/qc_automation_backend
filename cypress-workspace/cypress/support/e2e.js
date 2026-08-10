
import './commands'
import 'cypress-real-events/support';
import 'cypress-iframe';

afterEach(function () {
    const status = this.currentTest?.state ?? 'unknown';
    const testName = this.currentTest?.title ?? 'unknown';
    cy.task('writeLog', `Test: ${testName} | Status: ${status}`);
});


Cypress.on('uncaught:exception', (err, runnable) => {
    const ignoredMessages = [
        'ResizeObserver loop limit exceeded',
        'Non-Error promise rejection captured',
        'Script error.',
        'Cannot read properties of null',
    ];
    if (ignoredMessages.some(msg => err.message && err.message.includes(msg))) {
        return false;
    }
    return true;
});


