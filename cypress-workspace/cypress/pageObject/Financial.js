export class Financial {
    createInstrumentMasterACTypeSalesAccount() {
        cy.get('@inputData').then((data) => {
            const value = data[0].instrumentMaster;

            cy.handleSingleField('InstrumentName', value.name)
            cy.selectValueInPickListDrop('AccountName', value.accountTypeName)
            cy.selectRadioField('InstrumentAccountPostType', 'OnRealisation')
            cy.selectRadioField('InstrumentPostDatedType', 'OnDeposit')

            cy.enterInputValue('InstrumentValidityPeriodInDays', value.validityPeriodInDays)

            cy.selectRadioField('InstrumentStatusOfInstrument', 'Realisation')

            cy.clickCheckBox('InstrumentIsApplicableForBank')

            cy.clickCheckBox('InstrumentIsApplicableforCounterTransaction')

            cy.saveForm()
        })
    }
    getInstrumentMasterForm() {
        cy.get('@NewValue').then((data) => {
            cy.clickPickListDrop('InstrumentName')
            cy.get('.search-bar-container > .mat-icon').should('be.visible').click({ force: true })
            cy.get("[data-cy='PicklistSearchBar']").should('be.visible')
                .clear()
                .wait(1000)
                .type(data, { delay: 100 })
                .type("{enter}")
            cy.wait(1000)
            cy.get('.ag-row-odd > .ag-cell-value').click()

        })
        cy.wait(1000)
        cy.updateForm()
    }
    deleteInstrumentMasterForm() {
        cy.get('@NewValue').then((data) => {
            cy.clickPickListDrop('InstrumentName')
            cy.get('.search-bar-container > .mat-icon').should('be.visible').click({ force: true })
            cy.get("[data-cy='PicklistSearchBar']").should('be.visible')
                .clear()
                .type(data, { delay: 100 })
                .type("{enter}")
            cy.wait(1000)
            cy.get('.ag-row-odd > .ag-cell-value').click()
        })
        cy.wait(1000)
        cy.deleteForm()
    }
}
{/* <input type="checkbox" class="mdc-checkbox__native-control" 
id="mat-mdc-checkbox-3-input" tabindex="0" data-cy="InstrumentIsApplicableforCounterTransaction-CheckBox"></input> */}