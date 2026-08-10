
beforeEach(() => {

  const skipSpecs = [
    'Login',
    'SingleEmployeeExperience',
    'SingleEmployeeskill',
    'SingleEmployeeReference',
    'SingleEmployeeQualification',
    'SingleOutOfOfficeSettings',
    'Permission-Onduty(Hourwise)',
    'ESEmployeeNominee',
    'ApplyPermission'
  ];

  if (
    Cypress.spec.relative.includes('API_Tests') ||
    skipSpecs.some(spec => Cypress.spec.name.includes(spec))
  ) {
    cy.log('Skipping auto login');
    return;
  }

  cy.loginSession();

  });
