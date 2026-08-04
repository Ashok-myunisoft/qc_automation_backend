export class Inventory {
    createMachineSCreen() {
        cy.get('@inputData').then((data) => {
            const value = data[0].Machine;
            cy.selectValueInPickListDrop(value.ou, 'OrganizationUnitName')
            cy.selectValueInPickListDrop(value.type, 'MachineTypeName')
            cy.selectValueInPickListDrop(value.modelName, 'ModelName')
            cy.selectValueInPickListDrop(value.brandName, 'BrandName')
            cy.selectValueInPickListDrop(value.category, 'CategoryName')
            cy.selectValueInPickListDrop(value.subCategory, 'SubCategoryName')
            cy.selectAndVerifyComboBox('Item Serial Number', 'MachineItemLinkType')
            cy.selectValueInPickListDrop(value.item, 'ItemName')

            cy.get('.mat-mdc-tab-labels').contains('Profile').should('be.visible').click()

            cy.selectAndVerifyComboBox('2025', 'MachineYOM')
            cy.enterInputValue(value.mfrSlNo, 'MachineMfrSlNo')
            cy.selectValueInPickListDrop(value.supplier, 'SupplierName')
            cy.enterInputValue(value.mfrSlNo, 'MachineBillNumber')
            cy.enterInputValue(value.mfrSlNo, 'MachineLastNumber')
            cy.selectAndVerifyComboBox('Working', 'MachineMachineStatusValue')
            cy.enterInputValue(value.machineValue, 'MachineMachineValue')
            cy.setDate('12/FEB/2025', 'MachineBillDate')
            cy.setDate('18/FEB/2025', 'MachineInstallationDate')
            cy.setDate('13/FEB/2025', 'MachinePurchaseDate')
            cy.selectValueInPickListDrop(value.batchNumber, 'BatchNumber')
            cy.enterInputValue(value.machineValueInBill, 'MachineValueInBill')



            cy.get('.mat-mdc-tab-labels').contains('Production').should('be.visible').click()

            cy.enterInputValue(value.machineCapacity, 'MachineCapacity')
            cy.enterInputValue(value.lotvalue, 'MachineLotPerDay')
            cy.enterInputValue(value.efficiency, 'MachineEfficiency')
            cy.enterInputValue(value.minCapacity, 'MachineMinCapacity')
            cy.enterInputValue(value.speed, 'MachineSpeed')
            cy.enterInputValue(value.maxCapacity, 'MachineMaxCapacity')
            cy.selectValueInPickListDrop(value.uOMName, 'UOMName')
            cy.selectValueInPickListDrop(value.process, 'ProcessName')
            cy.selectValueInPickListDrop(value.workCenter, 'WorkCenterName')


            cy.get('.mat-mdc-tab-labels').contains('Service').should('be.visible').click()

            cy.selectAndVerifyComboBox('Extended Warranty', 'MachineServiceStatus')
            cy.selectValueInPickListDrop(value.serviceProvider, 'ServiceProviderName')
            cy.setDate('10/FEB/2025', 'MachineServiceFromDate')
            cy.setDate('13/FEB/2030', 'MachineServiceToDate')

        })
    }
}