export class CRMSales {

    createLcycle() {
        cy.get('@inputData').then((data) => {
            const value = data[0].LeadCycle
            cy.handleEntityDetails('LeadCycleCode', 'LeadCycleName', value.code, value.name)

            cy.selectAndVerifyComboBox('LeadCycleApplicableType', 'Only Lead')
        })
    }
}