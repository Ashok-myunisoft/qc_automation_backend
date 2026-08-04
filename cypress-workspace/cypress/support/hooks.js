// ERROR-05 fixed: removed unused import { Given, When, Then }

beforeEach(() => {

  const skipSpecs = [
    'Login',
    'SingleEmployeeExperience',
    'SingleEmployeeskill',
    'SingleEmployeeReference',
    'SingleEmployeeQualification',
    'SingleOutOfOfficeSettings',
    // 'Leave-Onduty(Daywise)',
    'Permission-Onduty(Hourwise)',
    'ESEmployeeNominee',
    'ApplyPermission'
  ];

  // Skip auto login for specific features and all API_Tests specs
  if (
    Cypress.spec.relative.includes('API_Tests') ||
    skipSpecs.some(spec => Cypress.spec.name.includes(spec))
  ) {
    cy.log('Skipping auto login');
    return;
  }

  // Reuse login session
  cy.loginSession();

  // ERROR-06 fixed: use relative path so baseUrl applies
  // ERROR-07 fixed: removed invalid waitUntil option
  // cy.visit('https://qcws.goodbookserp.in/5.5/welcome', {
  //   timeout: 120000,
  //   failOnStatusCode: false,
  });

//});
