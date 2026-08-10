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
    cy.get(`[data-cy="${codeEntity}-PickListDrop"]`).click();
    cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(code);
    cy.wait(1000);
    cy.get('.ag-center-cols-viewport').contains(`${code}(NEW)`).click();
    cy.wrap(code).as('NewCode');

    cy.wait(2000);

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
        .click();

    cy.get('[data-cy="PicklistSearchBar"]')
        .should('be.visible')
        .clear()
        .type(value);
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

                cy.wait(5000);

                cy.clickDataCy('DeleteForm');
                cy.get('div.alertbutton').click()

                cy.get(`[data-cy="${field}-PickListDrop"]`)
                    .scrollIntoView()
                    .should('be.visible')
                    .click();

                cy.get('[data-cy="PicklistSearchBar"]')
                    .should('be.visible')
                    .clear()
                    .type(value);

                cy.wait(1000);

                cy.get('.ag-center-cols-viewport')
                    .contains(newValue)
                    .should('be.visible')
                    .click();
            }

            cy.setData(field, value);

            cy.getData(field).then((data) => {
                cy.log(`Selected Value: ${data}`);
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









Cypress.Commands.add('setDate', (date, selector) => {

    cy.log(`Raw date value received: ${date}`);

    if (!date) {
        throw new Error('Date value is empty or undefined');
    }

    let normalizedDate = date;

    if (/^\d{2}\/\d{2}\/\d{4}$/.test(date)) {
        const [day, month, year] = date.split('/');
        normalizedDate = `${year}-${month}-${day}`;
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
    cy.get('.ag-center-cols-viewport')
        .eq(0).contains(`${value}(NEW)`).click();
})

Cypress.Commands.add('selectAndVerifyComboBox', (option, field) => {
    if (!option) {
        return;
    }
    cy.get(`[data-cy="${field}-ComboBox"]`).click();


    const normalizedOption = option.replace(/\s+/g, '').replace(/\b\w/g, char => char.toUpperCase());


    cy.get(`[data-cy="${normalizedOption}-ComboField"]`).click();

    cy.log(option)

    cy.get(`[data-cy="${field}-ComboBox"]`).within(() => {
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
})
Cypress.Commands.add('enterInputValue', (value, field) => {
    if (!value) {
        return;
    }
    cy.wait(100)
    cy.get(`[data-cy="${field}-Input"]`).focus().click({ force: true }).clear().type(value, { delay: 100 })

})

Cypress.Commands.add('selectValueInPickListDrop', (value, field) => {
    if (!value) {
        return;
    }

    cy.get(`[data-cy="${field}-PickListDrop"]`).click({ force: true });
    cy.wait(500);
    cy.get("[data-cy='PicklistSearchBar']").click().clear().type(value);
    cy.wait(2000);



    cy.contains('.ag-cell-value', value).click();

});

Cypress.Commands.add('selectCheckBoxValueInPickListDrop', (value, field) => {
    if (value === '') {
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
        return;
    }
    cy.get(`[data-cy="${id}-Textarea"]`).click().clear().type(value)
})

Cypress.Commands.add('setTimeInput', (selector, value) => {
    cy.get(`[data-cy="${selector}-Time"]`).should('be.visible').clear().type(value, { delay: 100 })
});

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












Cypress.Commands.add('SaveAndUpdateFormWithID', () => {
    cy.get('.DialogMessage')
        .invoke('text')
        .then((text) => {
            const regex = /With Id :-(\d+)/;
            const match = text.match(regex);

            if (match) {
                const extractedId = match[1];
                cy.wrap(extractedId).as('savedId');
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
                const extractedId = match[1];
                cy.wrap(extractedId).as('savedId');
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
    Cypress.env(key, value);
});

Cypress.Commands.add('getData', (key) => {
    return Cypress.env(key);
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








Cypress.Commands.add('runActivityTypeTests', () => {
    cy.task('readExcelFile', 'cypress/src/fixtures/activityType.xlsx').then((testData) => {
        testData.forEach((testCase) => {
            describe(`Condition: ${testCase.Conditions}`, () => {
                it(`should validate ${testCase.Conditions}`, () => {
                    cy.visit('/activity-type');

                    if (testCase.code)
                        cy.handleCodeEntityDetails('ActivityTypeCode', testCase.code)
                    if (testCase.name)
                        cy.handleNameEntityDetails('ActivityTypeName', testCase.name)
                    if (testCase.remarks)
                        cy.enterTextAreaValue('ActivityTypeRemarks', testCase.remarks);

                    cy.get('[data-cy="SaveForm"]').click()

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






Cypress.Commands.add(
    'verifyAPI',
    ({ alias, method, expectedStatus = 200, reqBody = {}, resBody = {} }) => {

        cy.wait(alias).then((interception) => {

            expect(interception.request.method).to.eq(method)

            expect(interception.response.statusCode).to.eq(expectedStatus)

            if (method !== 'GET' && Object.keys(reqBody).length > 0) {
                Object.keys(reqBody).forEach((key) => {
                    expect(interception.request.body[key]).to.eq(reqBody[key])
                })
            }

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
        body: {},
        failOnStatusCode: false
    });
});


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


Cypress.Commands.add('selectDropdownValue', (field, optionText) => {
    cy.get(`[data-cy="${field}-ComboBox"]`).click();
    const normalizedOption = optionText.replace(/\s+/g, '').replace(/\b\w/g, char => char.toUpperCase());
    cy.get('body').then(($body) => {
        const byCy = `[data-cy="${normalizedOption}-ComboField"]`;
        if ($body.find(byCy).length > 0) {
            cy.get(byCy).click();
        } else {
            cy.get('mat-option').contains(optionText).click();
        }
    });
});

Cypress.Commands.add('verifyFieldValue', (field, expectedValue) => {
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

Cypress.Commands.add('addPaymentTermGridRow', (index, type, days, percentage) => {
    if (index > 0) {
        cy.get('[data-cy="AddRow"]').click();
    }
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
    if (days !== undefined && days !== null) {
        cy.get(`[data-cy="PaymentTermDetailDays${index}-Input"]`).clear().type(days.toString());
    }
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



Cypress.Commands.add('loginToConnection', (connectionName) => {
    cy.task('loginToConnection', connectionName).then((dto) => {
        Cypress.env('loginDTO', dto);
        Cypress.env('serverConfigId', dto.ServerConfigId);
        Cypress.env('currentConnection', connectionName);
        if (dto.BaseURL) {
            Cypress.env('baseUrl', dto.BaseURL);
        }
        cy.log(`loginToConnection: connected as ${dto.UserName} → DB=${dto.DatabaseName} (ServerConfigId=${dto.ServerConfigId})`);
    });
});

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

        for (const [key, val] of Object.entries(expected)) {
            if (!ignoreSet.has(key.toUpperCase())) {
                expect(row[key], `Field ${key} in ${table}`).to.equal(val);
            }
        }

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

Cypress.Commands.add('cleanupDbRows', ({ table, whereColumn, prefix }) => {
    const serverConfigId = Cypress.env('serverConfigId');
    cy.task('queryDb', {
        serverConfigId,
        query: `DELETE FROM ${table} WHERE ${whereColumn} LIKE '${prefix}%'`,
    }).then((result) => {
        cy.log(`cleanupDbRows: deleted from ${table} WHERE ${whereColumn} LIKE '${prefix}%'`);
    });
});

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





