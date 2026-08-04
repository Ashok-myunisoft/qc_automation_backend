export class Quality {
    createStandardScreen() {
        cy.get('@inputData').then((data) => {
            const value = data[0].Standrad;
            cy.handleEntityDetails('StandardCode', 'StandardName', value.code, value.name)
            cy.enterInputValue('StandardNature', value.nature)
            cy.enterTextAreaValue('StandardRemarks', value.remarks)
            cy.clickDataCy('SaveForm')
            cy.contains('Details saved successfully').should('be.visible')
            cy.get('.mat-mdc-dialog-component-host > .mat-icon').click()
        })
        cy.get('[data-cy="AddNewForm"]').click()
    }
    createStandardTypeScreen() {
        cy.get('@inputData').then((data) => {
            const value = data[0].Standrad;
            cy.handleEntityDetails('StandardTypeCode', 'StandardTypeName', value.code, value.name)
        })
    }
    createAttributeTypeScreen() {
        cy.get('@inputData').then((data) => {
            const value = data[0].Standrad;
            cy.handleEntityDetails('AttributeTypeCode', 'AttributeTypeName', value.code, value.name)
        })
    }
}