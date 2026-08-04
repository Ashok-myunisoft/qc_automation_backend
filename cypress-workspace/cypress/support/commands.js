Cypress.Commands.add('Login', (Server) => {
    const { serverName, userName, password } = Cypress.env(Server);

    cy.visit('https://qcws.goodbookserp.in/5.5', { failOnStatusCode: false });
    cy.get('[data-cy="Database-Input"]').should('be.visible').clear().type(serverName)
    cy.get('[data-cy="Username-Input"]').should('be.visible').clear().type(userName)
    cy.get('[data-cy="Password-Input"]').should('be.visible').clear().type(password)
    cy.get('.signin-button').click()

    cy.wait(2000)
    cy.url().should('not.include', 'login');
    cy.location('pathname').should('eq', '/4.7/welcome');
    cy.get('.welcome-title').contains('Welcome to GoodBooks ERP').should('exist')

});
Cypress.Commands.add('loginInvalid', (Server) => {
    const { serverName, userName, password } = Cypress.env(Server);

    cy.visit('https://qcws.goodbookserp.in/5.5', {
        timeout: 300000,
        retryOnStatusCodeFailure: true,
        retryOnNetworkFailure: true,
        onBeforeLoad: (win) => {
            // Clear all storage before load
            win.sessionStorage.clear();
            win.localStorage.clear();
        },
        headers: {
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache'
        }
    });
    cy.get('[data-cy="Database-Input"]').should('be.visible').clear().type(serverName)
    cy.get('[data-cy="Username-Input"]').should('be.visible').clear().type(userName)
    cy.get('[data-cy="Password-Input"]').should('be.visible').clear().type(password)
    cy.get('.signin-button').click()

    cy.wait(2000)
    cy.url().should('not.include', 'login');
    cy.location('pathname').should('eq', '/4.7/welcome');
    cy.get('.welcome-title').contains('Welcome to GoodBooks ERP').should('exist')

});

Cypress.Commands.add('handleEntityDetails', (codeEntity, nameEntity, code, name) => {
    // Handle the code entity dropdown and search
    cy.get(`[data-cy="${codeEntity}-PickListDrop"]`).click();
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(code);
    cy.wait(1000);
    cy.get('.ag-center-cols-viewport').contains(`${code}(NEW)`).click();
    cy.wrap(code).as('NewCode');

    cy.wait(2000);

    // Handle the name entity dropdown and search
    cy.get(`[data-cy="${nameEntity}-PickListDrop"]`).click();
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(name);
    cy.wait(1000);
    cy.get('.ag-center-cols-viewport').contains(`${name}(NEW)`).click();
    cy.wrap(name).as('NewName');
});

Cypress.Commands.add('handleCodeEntityDetails', (field, value) => {
    cy.get(`[data-cy="${field}-PickListDrop"]`)
        .scrollIntoView()
        .should('be.visible')
        .click(); // Open picklist

    cy.get('[data-cy="PicklistSearchBar"]')
        .should('be.visible')
        .clear()
        .type(value); // Search for the value
    cy.wait(6000)
    cy.get('.ag-center-cols-viewport')
        .invoke('text')
        .then((text) => {
            const newValue = `${value}(NEW)`;

            if (text.includes(newValue)) {
                cy.log(`${newValue} found, selecting it.`);
                cy.get('.ag-center-cols-viewport')
                    .contains(newValue)
                    .should('be.visible')
                    .click();
            } else {
                cy.log(`${newValue} not found, selecting normal value.`);
                cy.get('.ag-center-cols-viewport')
                    .contains(value)
                    .should('be.visible')
                    .click();

                cy.wait(5000); // Allow the selection to complete

                // Delete the form
                cy.clickDataCy('DeleteForm');
                cy.get('div.alertbutton').click()
                //  cy.get('.mat-mdc-dialog-component-host > .mat-icon').click();

                // Re-open the picklist
                cy.get(`[data-cy="${field}-PickListDrop"]`)
                    .scrollIntoView()
                    .should('be.visible')
                    .click();

                // Re-search the value
                cy.get('[data-cy="PicklistSearchBar"]')
                    .should('be.visible')
                    .clear()
                    .type(value);

                cy.wait(1000); // Allow list to update

                // Select the NEW version
                cy.get('.ag-center-cols-viewport')
                    .contains(newValue)
                    .should('be.visible')
                    .click();
            }

            // Store the selected value
            cy.setData(field, value);

            cy.getData(field).then((data) => {
                cy.log(`Selected Value: ${data}`); // Ensure proper logging
            });
        });
})

Cypress.Commands.add('handleNameEntityDetails', (nameEntity, name) => {
    cy.get(`[data-cy="${nameEntity}-PickListDrop"]`).click();
    cy.wrap(name).as('NewName');
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(name);
    cy.get('.ag-center-cols-viewport').contains(`${name}(NEW)`).click();

})
Cypress.Commands.add('handleShortNameEntityDetails', (nameEntity, name) => {
    cy.get(`[data-cy="${nameEntity}-PickListDrop"]`).click();
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(name);
    cy.get('.ag-center-cols-viewport').contains(`${name}(NEW)`).click();

})
// Cypress.Commands.add('setDateInput', (selector, value) => {
//     cy.wait(1000)
//     // cy.get(`[data-cy="${selector}-DatePickerToggle"]`).should('be.visible')
//     cy.get(`[data-cy="${selector}-DatePickerToggle"] > .mdc-icon-button > .mat-mdc-button-touch-target`).should('be.visible')
//         .invoke('val', value)
//         .trigger('change');
// });
// Cypress.Commands.add('setDate', (date, selector) => {
//     // Click on the date picker toggle button
//     cy.get(`[data-cy="${selector}-DatePickerToggle"]`).click();

//     // Ensure the calendar popup is visible
//     cy.get('.mat-calendar').should('be.visible');

//     // Parse the date
//     const targetDate = new Date(date);
//     const targetYear = targetDate.getFullYear();
//     const targetMonth = targetDate.toLocaleString('default', { month: 'short' }).toUpperCase();
//     const targetDay = targetDate.getDate();

//     // Select the correct year if needed
//     cy.get('.mat-calendar-period-button').click();
//     cy.get('.mat-calendar-body').contains('.mat-calendar-body-cell-content', targetYear).click();
//     cy.get('.mat-calendar-previous-button > .mat-focus-indicator').click()

//     // Select the correct month
//     cy.get('.mat-calendar-body').contains('.mat-calendar-body-cell-content', targetMonth).click();

//     // Select the day
//     cy.get('.mat-calendar-body').contains('.mat-calendar-body-cell-content', targetDay).click();

//     // Verify the selected date (adjust the format based on application needs)
//     // const formattedDate = targetDate.toLocaleDateString('en-GB'); // Format: DD/MM/YYYY
//     // cy.get(`[data-cy="${selector}-DatePickerInput"]`).should('have.value', formattedDate);
// });

// // 


Cypress.Commands.add('setDate', (date, selector) => {

    cy.log(`Raw date value received: ${date}`);

    if (!date) {
        throw new Error('Date value is empty or undefined');
    }

    let normalizedDate = date;

    // ✅ HANDLE DD/MM/YYYY
    if (/^\d{2}\/\d{2}\/\d{4}$/.test(date)) {
        const [day, month, year] = date.split('/');
        normalizedDate = `${year}-${month}-${day}`; // YYYY-MM-DD
    }

    const targetDate = new Date(normalizedDate);

    if (isNaN(targetDate.getTime())) {
        throw new Error(`Invalid date format: ${date}`);
    }

    const targetYear = targetDate.getFullYear();
    const targetMonth = targetDate
        .toLocaleString('en-US', { month: 'short' })
        .toUpperCase();
    const targetDay = targetDate.getDate();

    cy.log(`Target Date: Year=${targetYear}, Month=${targetMonth}, Day=${targetDay}`);

    cy.get(`[data-cy="${selector}-DatePickerToggle"]`).click({ force: true });
    cy.get('.mat-calendar').should('be.visible');

    cy.get('.mat-calendar-period-button').click();

    const findYear = () => {
        cy.get('.mat-calendar-body').then($body => {
            const years = [...$body.find('.mat-calendar-body-cell-content')]
                .map(el => el.textContent.trim());

            if (!years.includes(targetYear.toString())) {
                cy.get('.mat-calendar-previous-button').click({ force: true });
                findYear();
            } else {
                cy.contains('.mat-calendar-body-cell-content', targetYear.toString()).click();
            }
        });
    };

    findYear();

    cy.contains('.mat-calendar-body-cell-content', targetMonth).click();
    cy.contains('.mat-calendar-body-cell-content', targetDay.toString()).click();
});


Cypress.Commands.add('clickDataCy', (value) => {
    cy.wait(1000)
    cy.get('.formbuttonbar')
        .find(`[data-cy="${value}"]`).click()


})

Cypress.Commands.add('handleSingleField', (EntityType, value) => {
    cy.get(`[data-cy="${EntityType}-PickListDrop"]`).should('be.visible').click()
    cy.get("[data-cy='PicklistSearchBar']").should('be.visible')
        .type(value, { delay: 100 });
    cy.wrap(value).as('NewValue');
    cy.wait(2000)
    //  cy.get('.mat-mdc-dialog-component-host > .ag-theme-material > .ag-root-wrapper > .ag-root-wrapper-body > .ag-root > .ag-body > .ag-body-viewport > .ag-center-cols-viewport')
    cy.get('.ag-center-cols-viewport')
        .eq(0).contains(`${value}(NEW)`).click();
})

Cypress.Commands.add('selectAndVerifyComboBox', (option, field) => {
    if (!option) {
        // Skip all actions if value is empty
        return;
    }
    cy.get(`[data-cy="${field}-ComboBox"]`).click();
    //cy.get('#mat-select-1-panel'||'#mat-select-2-panel').should('be.visible');


    const normalizedOption = option.replace(/\s+/g, '').replace(/\b\w/g, char => char.toUpperCase());


    cy.get(`[data-cy="${normalizedOption}-ComboField"]`).click();

    cy.log(option)

    cy.get(`[data-cy="${field}-ComboBox"]`).within(() => {
        // cy.get('#mat-select-value-1'||'#mat-select-value-2').should('contain', option);
        cy.contains(option)
    });
});
Cypress.Commands.add('selectAndVerifyComboBoxOption', (option, field) => {
    cy.get(`[data-cy="${field}-ComboBox"]`).click();
    const normalizedOption = option.replace(/\s+/g, '').replace(/\b\w/g, char => char.toUpperCase());
    cy.get(`[data-cy="${normalizedOption}-ComboOption"]`).click();

});

Cypress.Commands.add('clickRadioField', (radioBox, radioField) => {
    cy.get(`[data-cy="${radioBox}-RadioBox"]`).should('be.visible')
    cy.get(`[data-cy="${radioField}-RadioField"]`).should('be.visible').click()
})
Cypress.Commands.add('selectRadioField', (radioBox, radioField) => {
    if (!radioField) {
        // Skip all actions if value is empty
        return;
    }
    cy.wait(1000)
    cy.get(`[data-cy="${radioBox}-RadioBox"]`).find(`[data-cy="${radioField}-RadioField"]`).click()
})
Cypress.Commands.add('clickCheckBox', (value) => {
    cy.get(`[data-cy="${value}-UnCheckBox"] > .mdc-form-field > .mdc-checkbox > .mat-mdc-checkbox-touch-target`)
        .scrollIntoView()
        .should('be.visible').click({ force: true });
})
Cypress.Commands.add('clickGridCheckBox', (value) => {
    cy.get('#scrollableTable').scrollTo("right")
    cy.get(`[data-cy="${value}-UnCheckBox"] > .mdc-form-field > .mdc-checkbox > .mat-mdc-checkbox-touch-target`).click({ force: true });
    //cy.get('[data-cy="PayTaxTypeDetailIsDetail0-CheckBox"] > .mdc-form-field > .mdc-checkbox > .mat-mdc-checkbox-touch-target')
})
Cypress.Commands.add('enterInputValue', (value, field) => {
    if (!value) {
        // Skip all actions if value is empty
        return;
    }
    cy.wait(100)
    cy.get(`[data-cy="${field}-Input"]`).focus().click({ force: true }).clear().type(value, { delay: 100 })

})

Cypress.Commands.add('selectValueInPickListDrop', (value, field) => {
    if (!value) {
        // Skip all actions if value is empty
        return;
    }

    cy.get(`[data-cy="${field}-PickListDrop"]`).click({ force: true });
    cy.wait(500);
    //cy.get('.refresh').dblclick();
    cy.get("[data-cy='PicklistSearchBar']").click().clear().type(value);
    cy.wait(2000);

    // cy.get('[data-cy="PicklistGrid"] > .ag-root-wrapper > .ag-root-wrapper-body > .ag-root > .ag-body > .ag-body-viewport > .ag-center-cols-viewport > .ag-center-cols-container > .ag-row-even > .ag-column-first')
    //     .click();

    //  cy.contains('.ag-center-cols-container .ag-row', value).click({ force: true });

    // cy.contains('.ag-center-cols-container .ag-row', value)
    //   .scrollIntoView()
    //   .should('be.visible')
    //   .click();
    cy.contains('.ag-cell-value', value).click();

});

Cypress.Commands.add('selectCheckBoxValueInPickListDrop', (value, field) => {
    if (value === '') {
        // If value is empty, skip the picklist interaction and typing
        return;
    }

    cy.get(`[data-cy="${field}-PickListDrop"]`).click({ force: true });
    cy.wait(5000);

    cy.get("[data-cy='PicklistSearchBar']").click().clear().type(`${value}{enter}`);
    cy.wait(5000);

    cy.get('[data-cy="PicklistGrid"] > .ag-root-wrapper > .ag-root-wrapper-body > .ag-root > .ag-body > .ag-body-viewport > .ag-center-cols-viewport > .ag-center-cols-container > .ag-row-even > .ag-column-first')
        .click();

    cy.get('.select-btn').should('be.visible').click();

});
Cypress.Commands.add('clickPickListDrop', (testId) => {
    cy.wait(1000)
    cy.get(`[data-cy="${testId}-PickListDrop"]`).click()
})

Cypress.Commands.add('enterTextAreaValue', (id, value) => {
    if (!value) {
        // Skip all actions if value is empty
        return;
    }
    cy.get(`[data-cy="${id}-Textarea"]`).click().clear().type(value)
})

Cypress.Commands.add('setTimeInput', (selector, value) => {
    cy.get(`[data-cy="${selector}-Time"]`).should('be.visible').clear().type(value, { delay: 100 })
});
////save - update - delete

Cypress.Commands.add('deleteForm', () => {
    cy.clickDataCy('DeleteForm');
    cy.contains('Detail Deleted Successfully').should('be.visible')
    cy.get('.mat-mdc-dialog-component-host > .mat-icon').click()

});

Cypress.Commands.add('saveForm', () => {
    cy.clickDataCy('SaveForm')
    cy.contains('Details Saved Successfully').should('be.visible')
    cy.get('.mat-mdc-dialog-component-host > .mat-icon').click()
})
Cypress.Commands.add('updateForm', () => {
    cy.wait(1000)
    cy.clickDataCy('UpdateForm')
    cy.contains('Details Saved Successfully').should('be.visible')
    cy.get('.mat-mdc-dialog-component-host > .mat-icon').click()
})

// Cypress.Commands.add('SaveAndUpdateFormWithID', (button) => {
//     const buttonSelectors = {
//         'Save': 'SaveForm',
//         'Update': 'UpdateForm'
//     };

//     if (!buttonSelectors[button]) {
//         throw new Error(`Invalid button type: ${button}`);
//     }

//     cy.clickDataCy(buttonSelectors[button]);

//     // Ensure Cypress waits for the success message to appear
//     cy.contains('Details', { timeout: 5000 }).should('be.visible');

//     // Log the entire body text to see if the message is present
//     cy.get('body').invoke('text').then((fullText) => {
//         cy.log(`Full Body Text: ${fullText}`);
//         console.log(`Full Body Text: ${fullText}`);

//         // Regex to match different formats
//         const successMessageRegex = /Details (Saved|Updated) (Sucessfully|Successfully) (With|with) Id\s*[:=-]\s*(-?\d+)/;
//         const idMatch = fullText.match(successMessageRegex);

//         if (idMatch && idMatch[4]) {
//             const idValue = idMatch[4];

//             // Log extracted ID
//             cy.log(`Extracted ID: ${idValue}`);
//             console.log(`Extracted ID: ${idValue}`);

//             // Store ID reliably
//             cy.wrap(idValue).as('ExtractedID');

//             // Retrieve and log the stored ID
//             cy.get('@ExtractedID').then((data) => {
//                 cy.log(`Testing Extracted ID: ${data}`);
//                 console.log(`Testing Extracted ID: ${data}`);
//             });

//             // Close the dialog
//             cy.get('.mat-mdc-dialog-component-host > .mat-icon').click({ force: true });
//         } else {
//             cy.log("ID not found!").then(() => {
//                 throw new Error("Could not extract ID from message");
//             });
//         }
//     });
// });

Cypress.Commands.add('SaveAndUpdateFormWithID', () => {
    cy.get('.DialogMessage')
        .invoke('text')
        .then((text) => {
            const regex = /With Id :-(\d+)/;
            const match = text.match(regex);

            if (match) {
                const extractedId = match[1]; // Extract the ID
                cy.wrap(extractedId).as('savedId'); // Store as alias
                cy.log('Extracted ID:', extractedId);
            }
            cy.get('.mat-mdc-dialog-component-host > .mat-icon').click({ force: true });
        });

})
Cypress.Commands.add('SaveAndUpdateFormWithIDValue', () => {
    cy.get('.DialogMessage')
        .invoke('text')
        .then((text) => {
            const regex = /With Id-(\d+)/;
            const match = text.match(regex);

            if (match) {
                const extractedId = match[1]; // Extract the ID
                cy.wrap(extractedId).as('savedId'); // Store as alias
                cy.log('Extracted ID:', extractedId);
            }
            cy.get('.mat-mdc-dialog-component-host > .mat-icon').click({ force: true });
        });

})
Cypress.Commands.add('SaveAndUpdateFormWithName', () => {
    cy.get('.DialogMessage')
        .invoke('text')
        .then((fullText) => {
            expect(fullText).to.match(/Details (Saved|Updated) (Sucessfully|Successfully) With (Name|Number)/i);
            const displayedText = fullText.split(':')[1]?.trim()
            if (displayedText) {
                cy.wrap(displayedText).as('displayedText')
            }
            else {
                throw new Error('displayedText name not found while save or update sku setting screen')
            }
            cy.get('.mat-mdc-dialog-component-host > .mat-icon').click({ force: true });
        });


});
Cypress.Commands.add('clickButton', (button) => {
    cy.get(`[data-cy="${button}Form"]`).click()
})

Cypress.Commands.add('setData', (key, value) => {
    Cypress.env(key, value); // Stores data globally
});

Cypress.Commands.add('getData', (key) => {
    return Cypress.env(key); // Retrieves the stored value
});

Cypress.Commands.add('GetSavedForm', (savedValue, field) => {
    cy.get(`[data-cy="${field}-PickListDrop"]`).click();
    cy.get("[data-cy='PicklistSearchBar']").should('be.visible')
        .clear()
        .type(savedValue, { delay: 100 })
    cy.wait(8000)
    cy.get('[data-cy="PicklistGrid"]').contains(savedValue).click()
    cy.wait(3000)
})
// Cypress.Commands.add('UpdateName', (field, NewValue) => {
//     cy.get(`[data-cy="${field}-PickListDrop"]`).click();
//     cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(NewValue);
//     cy.wait(1000);
//     cy.get('.ag-center-cols-viewport').contains(`${NewValue}(NEW)`).click();
//     cy.wait(1000)
//     cy.get(`[data-cy="${field}-PickList"]`)
//         .should('be.visible')
//         .should('have.value', NewValue);
// })




// Cypress.Commands.add("loadTestData", (fileName, sheetName = "Sheet1") => {
//   const testData = getTestData(fileName, sheetName);
//   cy.wrap(testData).as("testData");
// });


// const XLSX = require('xlsx');

// Cypress.Commands.add('readExcelFile', (filePath) => {
//   return cy.task('readExcelFile', filePath);
// });

// cypress/support/commands.js
Cypress.Commands.add('runActivityTypeTests', () => {
    cy.task('readExcelFile', 'cypress/src/fixtures/activityType.xlsx').then((testData) => {
        testData.forEach((testCase) => {
            describe(`Condition: ${testCase.Conditions}`, () => {
                it(`should validate ${testCase.Conditions}`, () => {
                    // Navigate to page
                    cy.visit('/activity-type');

                    // Fill form based on test data
                    if (testCase.code)
                        cy.handleCodeEntityDetails('ActivityTypeCode', testCase.code)
                    if (testCase.name)
                        cy.handleNameEntityDetails('ActivityTypeName', testCase.name)
                    if (testCase.remarks)
                        cy.enterTextAreaValue('ActivityTypeRemarks', testCase.remarks);

                    // Submit form
                    cy.get('[data-cy="SaveForm"]').click()

                    // Verify results
                    const messages = testCase['Expected Message'].split(';');
                    messages.forEach(message => {
                        if (testCase.Conditions.startsWith('Valid')) {
                            cy.contains(message).should('be.visible');
                        } else {
                            cy.contains(message).should('be.visible');
                        }
                    });
                });
            });
        });
    });
});

Cypress.Commands.add('getTestData', (fileName, sheetName) => {
    return cy.task('readExcel', {
        filePath: `cypress/src/fixtures/${fileName}`,
        sheetName: sheetName
    });
});

//   Cypress.Commands.add('setCheckboxByDataCy', (dataCy, shouldCheck) => {
//     const checkboxSelector = `[data-cy="${dataCy}"] input[type="checkbox"]`;
//     cy.get(checkboxSelector).then(($checkbox) => {
//         if (shouldCheck && !$checkbox.is(':checked')) {
//             cy.wrap($checkbox).check({ force: true });
//         } else if (!shouldCheck && $checkbox.is(':checked')) {
//             cy.wrap($checkbox).uncheck({ force: true });
//         }
//     });
// });

// // cypress/support/commands.js
// Cypress.Commands.add('setCheckboxByDataCy', (selectorKey, value) => {
//     const action = value?.toLowerCase();

//     if (action === 'check') {
//       cy.get(`[data-cy="${selectorKey.replace('-CheckBox', '-UnCheckBox')}"]`)
//       .find('input[type="checkbox"]')
//       .click({ force: true });
//     } else if (action === 'uncheck') {
//       cy.get(`[data-cy="${selectorKey}"]`)
//       .find('input[type="checkbox"]')
//         .click({ force: true });
//     } else {
//       cy.log(`⚠️ No valid checkbox action provided for ${selectorKey}`);
//     }
//   });


//   Cypress.Commands.add('setCheckboxByDataCy', (baseSelector, expectedState) => {
//     const toCheck = expectedState.toLowerCase() === 'check';

//     const checkedSelector = `[data-cy="${baseSelector}-CheckBox"]`;
//     const uncheckedSelector = `[data-cy="${baseSelector}-UnCheckBox"]`;

//     cy.wait(300); // allow rendering time

//     cy.get('body').then(($body) => {
//         const isCurrentlyChecked = $body.find(checkedSelector).length > 0;
//         const isCurrentlyUnchecked = $body.find(uncheckedSelector).length > 0;

//         if (toCheck && isCurrentlyUnchecked) {
//             cy.log(`Checkbox ${baseSelector} is unchecked → Checking now`);
//             cy.get(uncheckedSelector).first().click({ force: true });
//         } else if (!toCheck && isCurrentlyChecked) {
//             cy.log(`Checkbox ${baseSelector} is checked → Unchecking now`);
//             cy.get(checkedSelector).first().click({ force: true });
//         } else {
//             cy.log(`Checkbox ${baseSelector} already in desired state`);
//         }
//     });
// });

// Cypress.Commands.add('setCheckboxByDataCy', (baseSelector, expectedState) => {
//     const toCheck = expectedState.toLowerCase() === 'check';

//     const checkSelector = `[data-cy="${baseSelector}-CheckBox"]`;      // Shown when currently UNCHECKED
//     const uncheckSelector = `[data-cy="${baseSelector}-UnCheckBox"]`;  // Shown when currently CHECKED

//     cy.wait(300); // Optional: ensure DOM is stable

//     cy.get('body').then(($body) => {
//         const shouldCheck = toCheck && $body.find(checkSelector).length > 0;
//         const shouldUncheck = !toCheck && $body.find(uncheckSelector).length > 0;

//         if (shouldCheck) {
//             cy.log(`Checkbox ${baseSelector} is unchecked → Checking now`);
//             cy.get(checkSelector).first().click({ force: true });
//         } else if (shouldUncheck) {
//             cy.log(`Checkbox ${baseSelector} is checked → Unchecking now`);
//             cy.get(uncheckSelector).first().click({ force: true });
//         } else {
//             cy.log(`Checkbox ${baseSelector} already in desired state`);
//         }
//     });
// });

// Cypress.Commands.add('setCheckboxByDataCy', (baseSelector, expectedState) => {
//     const toCheck = expectedState.toLowerCase() === 'check';
//     const uncheckSelector = `[data-cy="${baseSelector}-UnCheckBox"]`; // visible when checked
//     const checkSelector = `[data-cy="${baseSelector}-CheckBox"]`;     // visible when unchecked

//     cy.wait(300); // allow render

//     if (toCheck) {
//         cy.get('body').then(($body) => {
//             // only click if "UnCheckBox" (checked icon) is visible → needs to be checked
//             if ($body.find(uncheckSelector).length > 0) {
//                 cy.log(`Checkbox ${baseSelector} is unchecked → Checking now`);
//                 cy.get(uncheckSelector)
//                 .find('input[type="checkbox"]')
//                 .click({ force: true });
//             } else {
//                 cy.log(`Checkbox ${baseSelector} already checked → Skipping`);
//             }
//         });
//     } else {
//         cy.get('body').then(($body) => {
//             // only click if "CheckBox" (unchecked icon) is visible → needs to be unchecked
//             if ($body.find(checkSelector).length > 0) {
//                 cy.log(`Checkbox ${baseSelector} is checked → Unchecking now`);
//                 cy.get(checkSelector)
//                 .find('input[type="checkbox"]')
//                 .click({ force: true });
//             } else {
//                 cy.log(`Checkbox ${baseSelector} already unchecked → Skipping`);
//             }
//         });
//     }
// });


// Cypress.Commands.add('setCheckboxByDataCy', (baseSelector, expectedState) => {
//     const toCheck = expectedState.toLowerCase() === 'check';

//     const selectorToClickWhenChecked = `[data-cy="${baseSelector}-UnCheckBox"]`;  // Visible when checkbox is checked
//     const selectorToClickWhenUnchecked = `[data-cy="${baseSelector}-CheckBox"]`;  // Visible when checkbox is unchecked

//     cy.wait(300); // Allow rendering time
//     cy.log(selectorToClickWhenChecked)
//     cy.log(selectorToClickWhenUnchecked)
//     cy.get('body').then(($body) => {
//         const isCurrentlyChecked = $body.find(selectorToClickWhenChecked).length > 0;
//         const isCurrentlyUnchecked = $body.find(selectorToClickWhenUnchecked).length > 0;

//         if (toCheck && isCurrentlyUnchecked) {
//             cy.log(`Checkbox ${baseSelector} is unchecked → Checking now`);
//             cy.get(selectorToClickWhenUnchecked).first().click({ force: true });
//         } else if (!toCheck && isCurrentlyChecked) {
//             cy.log(`Checkbox ${baseSelector} is checked → Unchecking now`);
//             cy.get(selectorToClickWhenChecked).first().click({ force: true });
//         } else {
//             cy.log(`Checkbox ${baseSelector} already in desired state → Skipping`);
//         }
//     });
// });

Cypress.Commands.add('setCheckboxByDataCy', (baseSelector, expectedState) => {
    const toCheck = expectedState.toLowerCase() === 'check';

    cy.wait(300);

    cy.get(`[data-cy="${baseSelector}-UnCheckBox"], [data-cy="${baseSelector}-CheckBox"]`)
        .first()
        .find('input[type="checkbox"]')
        .then(($checkbox) => {
            const isChecked = $checkbox.prop('checked');

            if (toCheck && !isChecked) {
                cy.log(`Checkbox ${baseSelector} is unchecked → Checking now`);
                cy.wrap($checkbox).click({ force: true });
            } else if (!toCheck && isChecked) {
                cy.log(`Checkbox ${baseSelector} is checked → Unchecking now`);
                cy.wrap($checkbox).click({ force: true });
            } else {
                cy.log(`Checkbox ${baseSelector} already in desired state → Skipping`);
            }
        });
});

// Cypress.Commands.add('setCheckboxByDataCy', (selector, value) => {
//     cy.get(`[data-cy="${selector}"]`)
//       .then($checkbox => {
//         const isChecked = $checkbox.hasClass('mat-mdc-checkbox-checked');

//         if (value === 'TRUE' && !isChecked) {
//             cy.wrap($checkbox).click(); // Check it
//         } else if (value === 'FALSE' && isChecked) {
//             cy.wrap($checkbox).click(); // Uncheck it
//         }
//     });
// });



// Cypress.Commands.add('checkCheckboxByDataCy', (selector) => {
//     cy.get(`[data-cy="${selector}"]`)
//       .then($checkbox => {
//         const isChecked = $checkbox.hasClass('mat-mdc-checkbox-checked');
//         if (!isChecked) {
//             cy.wrap($checkbox).click(); // Only click if not already checked
//         }
//     });
// });

Cypress.Commands.add('uncheckCheckbox', (selector) => {
    cy.get(`[data-cy="${selector}-CheckBox"] input[type="checkbox"]`)
        .uncheck({ force: true });
});

Cypress.Commands.add('updateNameField', (field, NewValue) => {
    cy.get(`[data-cy="${field}-PickListDrop"]`).click();
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(NewValue);
    cy.wait(1000);
    cy.get('.ag-center-cols-viewport').contains(`${NewValue}(NEW)`).click();
    cy.wait(1000)
    cy.get(`[data-cy="${field}-PickList"]`)
        .should('be.visible')
        .should('have.value', NewValue);
})

Cypress.Commands.add('updateInputField', (field, value) => {
    cy.wait(100)
    cy.get(`[data-cy="${field}-Input"]`).focus().should('be.visible').click().clear()
        .should('have.value', '')
        .type(value, { delay: 100 })
    cy.wait(100)
    cy.get(`[data-cy="${field}-Input"]`).should('be.visible')
        .should('have.value', value);
})

Cypress.Commands.add('createdValueCheckingInAnotherPage', (value, field) => {
    cy.get(`[data-cy="${field}-PickListDrop"]`).click();
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(value);
    cy.wait(1000);
    cy.get('.ag-center-cols-viewport').should("have.value", value);
})

Cypress.Commands.add('updateNameValue', (field, NewValue) => {
    cy.get(`[data-cy="${field}-PickListDrop"]`).click();
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(NewValue);
    cy.wait(1000);
    cy.get('.ag-center-cols-viewport').contains(`${NewValue}(NEW)`).click();
    cy.wait(1000)
    cy.get(`[data-cy="${field}-PickList"]`)
        .should('be.visible')
        .should('have.value', NewValue);
})
Cypress.Commands.add('updateInputValue', (field, value) => {
    cy.wait(100)
    cy.get(`[data-cy="${field}-Input"]`).focus().should('be.visible').click().clear()
        .should('have.value', '')
        .type(value, { delay: 100 })
    cy.wait(100)
    cy.get(`[data-cy="${field}-Input"]`).should('be.visible')
        .should('have.value', value);
})

Cypress.Commands.add('updateTextareaValue', (field, NewValue) => {
    cy.get(`[data-cy="${field}-Textarea"]`).should('be.visible').clear().type(NewValue)
        .should('have.value', NewValue);
})

Cypress.Commands.add('verifyPickListValue', (fieldId, expectedValue) => {
    cy.get(`[data-cy="${fieldId}-PickList"]`)
        .invoke('val')
        .should('eq', expectedValue);
})
Cypress.Commands.add('verifyInputValue', (field, expectedValue) => {
    cy.get(`[data-cy="${field}-Input"]`)
        .should('have.value', expectedValue);
})

Cypress.Commands.add('verifyTextareaValue', (field, expectedValue) => {
    cy.get(`[data-cy="${field}-Textarea"]`)
        .should('have.value', expectedValue);
})
Cypress.Commands.add('verifyComboBoxValue', (fieldId, expectedDisplayValue) => {
    cy.get(`[data-cy="${fieldId}-ComboBox"]`)
        .should('contain.text', expectedDisplayValue);
})
Cypress.Commands.add('verifyDatePickerToggle', (field, expectedValue) => {
    cy.get(`[data-cy="${field}-DatePickerToggle"]`)
        .should('have.value', expectedValue);
})

// Cypress.Commands.add('ToggleButton', (toggleField) => {
//     cy.get(`[data-cy="${toggleField}-ToggleButton"]`).should('be.visible').click({ force: true });
// })





Cypress.Commands.add(
    'verifyAPI',
    ({ alias, method, expectedStatus = 200, reqBody = {}, resBody = {} }) => {

        cy.wait(alias).then((interception) => {

            // Method check
            expect(interception.request.method).to.eq(method)

            // Status check
            expect(interception.response.statusCode).to.eq(expectedStatus)

            // Request body validation
            if (method !== 'GET' && Object.keys(reqBody).length > 0) {
                Object.keys(reqBody).forEach((key) => {
                    expect(interception.request.body[key]).to.eq(reqBody[key])
                })
            }

            // Response body validation
            if (Object.keys(resBody).length > 0) {
                Object.keys(resBody).forEach((key) => {
                    expect(interception.response.body[key]).to.eq(resBody[key])
                })
            }
        })
    }
)



Cypress.Commands.add(
    'verifyGetAPI',
    ({ url, expectedStatus = 200, expectedData = {} }) => {

        cy.request({
            method: 'GET',
            url,
            failOnStatusCode: false
        }).then((response) => {

            expect(response.status).to.eq(expectedStatus)

            if (expectedStatus === 200) {
                Object.keys(expectedData).forEach((key) => {
                    expect(response.body[key]).to.eq(expectedData[key])
                })
            }
        })
    }
)



//////////////////


// // Cypress.Commands.add(
// //   'postApi',
// //   (baseUrl, endpoint, requestBody, headers = {}) => {
// //     return cy.request({
// //       method: 'POST',
// //       url: `${baseUrl}${endpoint}`,
// //       body: requestBody,
// //       headers: {
// //         'Content-Type': 'application/json',
// //         ...headers
// //       },
// //       failOnStatusCode: false
// //     })
// //   }
// // )

// Cypress.Commands.add('saveCheckingApi', (baseUrl, endpoint, requestBody) => {

//     const loginDTO = {
//         "AdminRights": 0, "CounterOperationId": -1, "UserCode": "2501", "DatabaseName": "unisoftgb4", "SessionId": 0, "ValidToTime": "MDktMTItMjAyNSAyMDoyNjozMQ==", "ValidityOfSession": "LTE0OTk5OTk5OTgtMTM5OTk5OTc2MC0xNTAwMDAwMDAwLTEtMU1Ea3RNVEl0TWpBeU5TQXlNRG95Tmpvek1RPT0xNzIuMTYuMjAwLjM4OjgwLTE=", "UserId": -1499999998, "UserPrimaryMailId": "edpadmin@rabwin.in", "ServerId": -1399999760, "RoleId": -1500000000, "AppId": -1, "DeviceId": -1, "WorkOUId": -1499999996, "WorkPeriodId": -1899999998, "WorkPartyBranchId": -1, "WorkStoreId": -1, "Realm": "Private Data", "ImgPath": "../images/user/", "ModeOfOperation": 3, "MachineIP": "192.168.0.1", "UserName": "Sivakumar K", "TimeZone": 330, "DatabaseOffset": 0, "DatabaseType": 0, "UserCriteriaConfigId": -1, "ClientId": -1399999725, "SourceType": 5, "StartTime": "911112548", "DateFormat": "dd/MM/yyyy", "CurrencyFormat": "#,##,###.##", "TimeFormat": "HH:MM", "QuantityFormat": "###0.00", "FromMailMenuId": "-1", "FromMailvalueId": "-1", "FEUri": "/fws/UserService/AuthenticateUser", "IsAdmin": false, "ServerConfigId": -1399999704, "ModeOfWorking": 0, "BaseUri": "172.16.200.38:80", "Geo": null, "LoginServerDate": "2025-12-09T11:25:48.2588199+05:30", "LoginEventLogId": -1, "ServerMachineName": "RABWIN:HELPDESKDB:LIVE", "ServerName": "172.16.200.39", "ServerIP": "172.16.200.39", "ServerUniqueDetails": "00:50:56:98:92:61", "ServerPopup": "", "WorkFinanceBookId": -1, "OuCode": "RABINT", "OuName": "RABWIN INTELLIGENT", "SelectlistOperationType": 6, "LastLoginUsedTime": "MDktMTItMjAyNSAxMToyNTo0OA==", "ExpiryTime": 540, "GraceTime": 15, "ServerConfigOffset": 0, "ServerConfigMaxValue": 0, "TimeZoneId": 91, "TimeZoneDisplayName": "(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi", "ServiceOffSet": 330, "FinalOffSetValue": 660, "FETIMEZONEOFFSET": -330, "IsIpBasedCheckingRequired": 1, "IsForcePasswordChange": 1, "Delimiter": ",", "ReportBaseUri": "", "FEVersion": 4, "LanguageId": "", "AttachmentOption": 2, "BaseURL": "http://172.16.200.38:80/gb4", "IsValidationRequired": 1, "KeycloakUrl": "http://localhost:5000/Keycloak/KeyCloakAuthenticateUser", "GB5Enabled": 0, "WorkPeriodFromDate": "/Date(1743445800000)/", "WorkPeriodToDate": "/Date(1774895400000)/"
//     };

//     return cy.request({
//         method: 'POST',
//         url: `${baseUrl}${endpoint}`,
//         headers: {
//             'Content-Type': 'application/json',
//             'Login': JSON.stringify(loginDTO)   // ✅ MUST be "Login"
//         },
//         body: requestBody,                   // ✅ only business data
//         failOnStatusCode: false
//     });
// });


// Cypress.Commands.add('getDepartmentApi', (baseUrl, departmentId) => {

//   return cy.request({
//     method: 'GET',
//     url: `${baseUrl}/ads/Department/GetDepartment/?DepartmentId=${departmentId}`,
//     headers: {
//       'Content-Type': 'application/json',
//       'Login': JSON.stringify(Cypress.env('loginDTO'))
//     },
//     failOnStatusCode: false
//   });
// });





// 🔹 COMMON POST (Save / Update)
Cypress.Commands.add('apiPost', (baseUrl, endpoint, requestBody) => {

    return cy.request({
        method: 'POST',
        url: `${baseUrl}${endpoint}`,
        headers: {
            'Content-Type': 'application/json',
            'Login': JSON.stringify(Cypress.env('loginDTO'))
        },
        body: requestBody,
        failOnStatusCode: false
    });
});

Cypress.Commands.add('apiGetSelectListPost', (baseUrl, endpoint, requestBody) => {


    return cy.request({
        method: 'POST',
        url: `${baseUrl}${endpoint}`,
        headers: {
            'Content-Type': 'application/json',
            'Login': JSON.stringify(Cypress.env('loginDTO'))
        },
        body: requestBody,
        failOnStatusCode: false
    });

});


// 🔹 COMMON PUT (Update)
Cypress.Commands.add('apiUpdate', (baseUrl, endpoint, requestBody) => {

    return cy.request({
        method: 'POST',
        url: `${baseUrl}${endpoint}`,
        headers: {
            'Content-Type': 'application/json',
            'Login': JSON.stringify(Cypress.env('loginDTO'))
        },
        body: requestBody,
        failOnStatusCode: false
    });
});

// 🔹 COMMON GET
// Note: no body / no Content-Type on GET — GB5 rejects GET with a JSON body.
Cypress.Commands.add('apiGet', (url) => {

    return cy.request({
        method: 'GET',
        url: url,
        headers: {
            'Login': JSON.stringify(Cypress.env('loginDTO'))
        },
        failOnStatusCode: false
    });

});



Cypress.Commands.add('apiGetPicklist', (baseUrl, endpoint) => {
    return cy.request({
        method: 'GET',
        url: `${baseUrl}${endpoint}`,
        headers: {
            'Content-Type': 'application/json',
            'Login': JSON.stringify(Cypress.env('loginDTO'))
        },
        body: {},                // GET with empty body
        failOnStatusCode: false
    });
});


// 🔹 COMMON DELETE
Cypress.Commands.add('apiDelete', (fullUrl, body = {}) => {

    return cy.request({
        method: 'DELETE',
        url: fullUrl,
        body: body,
        headers: {
            'Content-Type': 'application/json',
            'Login': JSON.stringify(Cypress.env('loginDTO'))
        },
        failOnStatusCode: false
    });
});

// 🔹 COMMON PUT (state-change endpoints: ReviewDdlScript, ApproveDdlScript, ApproveChangeRequest)
Cypress.Commands.add('apiPut', (fullUrl, body = {}) => {

    return cy.request({
        method: 'PUT',
        url: fullUrl,
        body: body,
        headers: {
            'Content-Type': 'application/json',
            'Login': JSON.stringify(Cypress.env('loginDTO'))
        },
        failOnStatusCode: false
    });
});





// Cypress.Commands.add('apiSeparateDelete', (baseUrl, endpoint, body) => {
//     return cy.request({
//         method: 'DELETE',
//         url: `${baseUrl}${endpoint}`,
//         headers: {
//             'Content-Type': 'application/json',
//             'Login': JSON.stringify(Cypress.env('loginDTO'))
//         },
//         body: body, // <--- REQUIRED
//         failOnStatusCode: false
//     });
// });




Cypress.Commands.add('apiSeparateDelete', (baseUrl, endpoint, deleteId) => {

    const fullUrl = `${baseUrl}${endpoint}${deleteId}`;
    cy.request({
        method: 'DELETE',
        url: fullUrl,
        body: { CheckingId: deleteId },
        headers: {
            "Content-Type": "application/json",
            "Login": JSON.stringify(Cypress.env('loginDTO'))
        },
        failOnStatusCode: false
    })
});




Cypress.Commands.add('loginSession', () => {
    // cy.session('user-login', () => {

    cy.visit('https://qcws.goodbookserp.in/5.5', {
        timeout: 120000,
        failOnStatusCode: false,
    });

    cy.get('.login-btn', { timeout: 60000 }).click();

    cy.get('[data-cy="Database-Input"]', { timeout: 30000 })
        .should('be.visible')
        .type('BASICTEST');

    cy.get('.btn-submit').click();

    cy.get('[data-cy="Username-Input"]', { timeout: 30000 })
        .should('be.visible')
        .type('E544');

    cy.get('[data-cy="Password-Input"]')
        .type('12345', { log: false });

    cy.get('.signin-button').click();

    cy.get('.welcome-title', { timeout: 60000 })
        .should('be.visible');
});
//});


// Select a value from a ComboBox/dropdown by field name and option text
Cypress.Commands.add('selectDropdownValue', (field, optionText) => {
    cy.get(`[data-cy="${field}-ComboBox"]`).click();
    const normalizedOption = optionText.replace(/\s+/g, '').replace(/\b\w/g, char => char.toUpperCase());
    // Try data-cy option first, fallback to mat-option text match
    cy.get('body').then(($body) => {
        const byCy = `[data-cy="${normalizedOption}-ComboField"]`;
        if ($body.find(byCy).length > 0) {
            cy.get(byCy).click();
        } else {
            cy.get('mat-option').contains(optionText).click();
        }
    });
});

// Verify the value of a picklist or input field
Cypress.Commands.add('verifyFieldValue', (field, expectedValue) => {
    // Try PickList input first, then plain Input
    cy.get('body').then(($body) => {
        if ($body.find(`[data-cy="${field}-PickList"]`).length > 0) {
            cy.get(`[data-cy="${field}-PickList"]`).should('have.value', expectedValue);
        } else if ($body.find(`[data-cy="${field}-Input"]`).length > 0) {
            cy.get(`[data-cy="${field}-Input"]`).should('have.value', expectedValue);
        } else {
            cy.get(`[data-cy="${field}-ComboBox"]`).should('contain', expectedValue);
        }
    });
});

// Add a row to the PaymentTerm grid
Cypress.Commands.add('addPaymentTermGridRow', (index, type, days, percentage) => {
    if (index > 0) {
        cy.get('[data-cy="AddRow"]').click();
    }
    // Payment type dropdown
    if (type) {
        const normalizedType = type.replace(/\s+/g, '').replace(/\b\w/g, c => c.toUpperCase());
        cy.get(`[data-cy="PaymentTermDetailPaymentType${index}-ComboBox"]`).click();
        cy.get('body').then(($body) => {
            const byCy = `[data-cy="${normalizedType}${index}-ComboOption"]`;
            if ($body.find(byCy).length > 0) {
                cy.get(byCy).click();
            } else {
                cy.get('mat-option').contains(type).click();
            }
        });
    }
    // Days field
    if (days !== undefined && days !== null) {
        cy.get(`[data-cy="PaymentTermDetailDays${index}-Input"]`).clear().type(days.toString());
    }
    // Percentage field
    if (percentage !== undefined && percentage !== null) {
        cy.get(`[data-cy="PaymentTermDetailPercentage${index}-Input"]`).clear().type(percentage.toString());
    }
});

Cypress.Commands.add('loginSessionE479', () => {
    cy.session('user-login-E479', () => {

        cy.visit('https://qcws.goodbookserp.in/5.5', {
            timeout: 120000,
            failOnStatusCode: false,
        });

        cy.get('.login-btn', { timeout: 60000 })
            .should('be.visible')
            .click();

        cy.get('[data-cy="Database-Input"]', { timeout: 30000 })
            .should('be.visible')
            .clear()
            .type('BASICTEST');

        cy.get('.btn-submit').click();

        cy.get('[data-cy="Username-Input"]', { timeout: 30000 })
            .should('be.visible')
            .clear()
            .type('E479');

        cy.get('[data-cy="Password-Input"]')
            .clear()
            .type('12345', { log: false });

        cy.get('.signin-button').click();

        cy.get('.welcome-title', { timeout: 60000 })
            .should('be.visible');
    });
});


// ═══════════════════════════════════════════════════════════════════════════════
// API TEST LAYER COMMANDS
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * cy.loginToConnection(connectionName)
 *
 * Calls the GoodBooks login endpoint for the named test connection and stores
 * the returned LoginDTO in Cypress.env so subsequent cy.apiPost / cy.apiGet
 * calls automatically use the right session.
 *
 * Also stores `serverConfigId` for use in cy.assertDbRow / cy.task('queryDb').
 *
 * Connection credentials come from cypress.env.json TestConnections map.
 *
 * @example
 *   cy.loginToConnection('BasicTest');
 *   // Now Cypress.env('loginDTO') and Cypress.env('serverConfigId') are set.
 */
Cypress.Commands.add('loginToConnection', (connectionName) => {
    cy.task('loginToConnection', connectionName).then((dto) => {
        Cypress.env('loginDTO', dto);
        Cypress.env('serverConfigId', dto.ServerConfigId);
        Cypress.env('currentConnection', connectionName);
        // BaseURL from LoginDTO (e.g. "http://192.168.0.112:85/gb4") drives cy.apiPost/apiGet/apiDelete
        if (dto.BaseURL) {
            Cypress.env('baseUrl', dto.BaseURL);
        }
        cy.log(`loginToConnection: connected as ${dto.UserName} → DB=${dto.DatabaseName} (ServerConfigId=${dto.ServerConfigId})`);
    });
});

/**
 * cy.assertDbRow({ table, where, expected, ignore })
 *
 * Queries the current test database (via cy.task queryDb) for a row matching
 * the `where` conditions, then asserts every field in `expected`.
 *
 * Non-deterministic fields (CREATEDON, MODIFIEDON, VERSION, SORTORDER, *ID
 * system fields) are automatically skipped unless you explicitly include them
 * in `expected`.  Timestamps are asserted to be within 30 seconds of now.
 *
 * @param {object} opts
 * @param {string}   opts.table    — DB table name, e.g. 'MBRANCH'
 * @param {object}   opts.where    — WHERE clause key/value pairs (simple equality)
 * @param {object}   opts.expected — Field/value pairs to assert on the found row
 * @param {string[]} [opts.ignore] — Extra field names to skip in assertion
 *
 * @example
 *   cy.assertDbRow({
 *     table:    'MBRANCH',
 *     where:    { BRANCHCODE: 'TEST-API-BR-001' },
 *     expected: { BRANCHNAME: 'North Branch', ISACTIVE: true },
 *   });
 */
Cypress.Commands.add('assertDbRow', ({ table, where, expected, ignore = [] }) => {
    const DEFAULT_IGNORE = new Set([
        'CREATEDON', 'MODIFIEDON', 'CREATEDBYID', 'MODIFIEDBYID',
        'VERSION', 'SORTORDER',
    ]);
    const ignoreSet = new Set([...DEFAULT_IGNORE, ...ignore.map(f => f.toUpperCase())]);

    const serverConfigId = Cypress.env('serverConfigId');

    const whereClause = Object.entries(where)
        .map(([k, v]) => `${k} = '${v}'`)
        .join(' AND ');

    cy.task('queryDb', {
        serverConfigId,
        query: `SELECT * FROM ${table} WHERE ${whereClause}`,
    }).then((rows) => {
        expect(rows, `assertDbRow: expected a row in ${table} WHERE ${whereClause}`)
            .to.have.length.greaterThan(0);

        const row = rows[0];

        // Assert expected field values (skip auto-ignored fields)
        for (const [key, val] of Object.entries(expected)) {
            if (!ignoreSet.has(key.toUpperCase())) {
                expect(row[key], `Field ${key} in ${table}`).to.equal(val);
            }
        }

        // Assert timestamp fields are recent (within 30 s)
        const now = Date.now();
        for (const tsField of ['CREATEDON', 'MODIFIEDON']) {
            if (row[tsField] != null) {
                const ts = new Date(row[tsField]).getTime();
                const diffMs = Math.abs(now - ts);
                expect(diffMs, `${tsField} should be within 30 s of now (diff: ${diffMs}ms)`)
                    .to.be.lessThan(30000);
            }
        }
    });
});

/**
 * cy.cleanupDbRows({ table, whereColumn, prefix })
 *
 * Deletes test rows from a table whose `whereColumn` starts with `prefix`.
 * Used in afterEach cleanup hooks and explicit cleanup steps.
 *
 * @example
 *   cy.cleanupDbRows({ table: 'MBRANCH', whereColumn: 'BRANCHCODE', prefix: 'TEST-API-' });
 */
Cypress.Commands.add('cleanupDbRows', ({ table, whereColumn, prefix }) => {
    const serverConfigId = Cypress.env('serverConfigId');
    cy.task('queryDb', {
        serverConfigId,
        query: `DELETE FROM ${table} WHERE ${whereColumn} LIKE '${prefix}%'`,
    }).then((result) => {
        cy.log(`cleanupDbRows: deleted from ${table} WHERE ${whereColumn} LIKE '${prefix}%'`);
    });
});

/**
 * cy.cleanupDbRowsCascade({ parentTable, childTable, fkColumn, whereColumn, prefix })
 *
 * Deletes child rows first (via FK subquery), then parent rows — avoids FK constraint errors.
 * Use when SaveAPI creates child records in a related table (e.g. MCOSTCENTERPATTERNDETAIL).
 *
 * @example
 *   cy.cleanupDbRowsCascade({
 *     parentTable: 'MCOSTCENTERPATTERN',
 *     childTable:  'MCOSTCENTERPATTERNDETAIL',
 *     fkColumn:    'COSTCENTERPATTERNID',
 *     whereColumn: 'COSTCENTERPATTERNCODE',
 *     prefix:      'tccp001',
 *   });
 */
Cypress.Commands.add('cleanupDbRowsCascade', ({ parentTable, childTable, fkColumn, whereColumn, prefix }) => {
    const serverConfigId = Cypress.env('serverConfigId');
    return cy.task('queryDb', {
        serverConfigId,
        query: `DELETE FROM ${childTable} WHERE ${fkColumn} IN (SELECT ${fkColumn} FROM ${parentTable} WHERE ${whereColumn} LIKE '${prefix}%')`,
    }).then(
        () => cy.log(`cleanupDbRowsCascade: child ${childTable} cleared`),
        (err) => cy.log(`WARN: child cascade cleanup failed (${err?.message ?? err}) — continuing`)
    ).then(() => {
        return cy.task('queryDb', {
            serverConfigId,
            query: `DELETE FROM ${parentTable} WHERE ${whereColumn} LIKE '${prefix}%'`,
        }).then(
            () => cy.log(`cleanupDbRowsCascade: parent ${parentTable} WHERE ${whereColumn} LIKE '${prefix}%' cleared`),
            (err) => cy.log(`WARN: parent cleanup failed (${err?.message ?? err}) — continuing`)
        );
    });
});







