export class Admin {

    createActivityType(condition) {
        cy.get("@excelData").then((data) => {
            const rowData = data.find((row) => row["Conditions"] === condition);

            if (!rowData) {
                throw new Error(`Condition '${condition}' not found in Excel data.`);
            }
            if (rowData["code"]) {
                cy.handleCodeEntityDetails('ActivityTypeCode', rowData["code"])
            }
            if (rowData["name"]) {
                cy.handleNameEntityDetails('ActivityTypeName', rowData["name"])
            }
            if (rowData["remarks"]) {
                cy.enterTextAreaValue('ActivityTypeRemarks', rowData["remarks"])
            }
        })
    }
    validationMessageVerify(condition) {
        cy.get("@excelData").then((data) => {
            // Find the row that matches the given condition
            const rowData = data.find(row => row["Conditions"] === condition);

            if (!rowData) {
                throw new Error(`Condition '${condition}' not found in Excel data`);
            }

            const expectedMessages = rowData["Message"].split(/\s*,\s*/); // Handle multiple messages

            // Wait for the dialog box to appear
            cy.get("gb-dialogbox", { timeout: 10000 }).should("be.visible");

            cy.get("gb-dialogbox").within(() => {
                expectedMessages.forEach((message) => {
                    cy.contains(message.trim()).should("be.visible");
                });
            });

            // Close the dialog box if the close button is visible
            cy.get('.mat-mdc-dialog-component-host > .mat-icon')
                .should("be.visible")
                .should("not.be.disabled")
                .click();
        });
    }
    getSavedFormValue(CodeId,condition) {
        cy.get("@excelData").then((data) => {
            const rowData = data.find((row) => row["Conditions"] === condition);
            cy.get(`[data-cy="${CodeId}-PickListDrop"]`).click()
            cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(rowData["code"]);
            cy.wait(1000);
            cy.get('.ag-center-cols-viewport').contains(rowData["code"]).click();
        })
    }
}