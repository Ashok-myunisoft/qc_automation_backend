export class common {
    // clickModulesAndScreens(dataTable) {
    //     // cy.get('.close > .mat-icon').click({ force: true })
    //     cy.get('.checkbox-container').click()
    //     cy.get('.mat-mdc-dialog-actions > button').click()
    //     const navigatePath = dataTable.hashes()[0];
    //     cy.wait(3000)
    //     cy.get('[data-cy="ModuleList"] > .mat-icon').should('be.visible').and('not.be.disabled').should('be.visible').dblclick()
    //     if (navigatePath.Module) {

    //         cy.get(`[data-cy="${navigatePath.Module}-Module"]`).click()
    //     }
    //     if (navigatePath.Folder) {

    //         cy.get(`[data-cy="${navigatePath.Folder}-MenuFolder0"]`).click()
    //     }
    //     if (navigatePath.Screen) {

    //         cy.get(`[data-cy="${navigatePath.Screen}-MenuData1"]`).click()
    //     }
    //     if (navigatePath.SubFolder) {
    //         cy.get(`[data-cy="${navigatePath.SubFolder}-MenuFolder1"]`).click()
    //     }
    //     if (navigatePath.SubScreen) {

    //         cy.get(`[data-cy="${navigatePath.SubScreen}-MenuData2"]`).click()
    //     }
    //     cy.get('.navbtn > .mat-icon').click()
    // }

    clickModulesAndScreens(dataTable) {
        cy.get('.checkbox-container').click()
        cy.get('.mat-mdc-dialog-actions > button').click()
        const navigatePath = dataTable.hashes()[0];
        cy.wait(5000)
        cy.get('[data-cy="ModuleList"] > .mat-icon').should('be.visible').and('not.be.disabled').should('be.visible').dblclick()
        if (navigatePath.Module) {
            cy.get(`[data-cy="${navigatePath.Module}-Module"]`).click()
        }
        if (navigatePath.Folder) {
            cy.get(`[data-cy="${navigatePath.Folder}-MenuFolder0"]`).click()
        }
        if (navigatePath.Screen) {
            cy.get(`[data-cy="${navigatePath.Screen}-MenuData0"]`).click()
        }
        if (navigatePath.SubFolder) {
            cy.get(`[data-cy="${navigatePath.SubFolder}-MenuFolder1"]`).click()
        }
        if (navigatePath.SubScreen) {
            cy.get(`[data-cy="${navigatePath.SubScreen}-MenuData1"]`).click()
        }
        cy.get('.navbtn > .mat-icon').click()
    }
    clickModulesAndScreens1(dataTable) {
        const navigatePath = dataTable.hashes()[0];
        cy.wait(3000)
        cy.get('.logo').click()
        cy.get('[data-cy="ModuleList"] > .mat-icon').should('be.visible').and('not.be.disabled').should('be.visible').dblclick()
        if (navigatePath.Module) {

            cy.get(`[data-cy="${navigatePath.Module}-Module"]`).click()
        }
        if (navigatePath.Folder) {

            cy.get(`[data-cy="${navigatePath.Folder}-MenuFolder0"]`).click()
        }
        if (navigatePath.Screen) {

            cy.get(`[data-cy="${navigatePath.Screen}-MenuData1"]`).click()
        }
        if (navigatePath.SubFolder) {
            cy.get(`[data-cy="${navigatePath.SubFolder}-MenuFolder1"]`).click()
        }
        if (navigatePath.SubScreen) {

            cy.get(`[data-cy="${navigatePath.SubScreen}-MenuData2"]`).click()
        }
    }

    RetrieveTheForm(pickList, value) {
        cy.clickPickListDrop(pickList)
        cy.get(`@${value}`).then((data) => {
            cy.log(data)
            cy.get("[data-cy='PicklistSearchBar']").should('be.visible')
                .clear()
                .type(data, { delay: 100 })
                .type("{enter}")
            cy.wait(1000)
            cy.get('.ag-row-odd > .ag-cell-value').eq(1).click()

        })
        cy.wait(1000)
        cy.updateForm()
    }
    DeleteTheForm(pickList, value) {
        cy.clickPickListDrop(pickList)
        cy.get(`@${value}`).then((data) => {
            cy.get("[data-cy='PicklistSearchBar']").should('be.visible')
                .clear()
                .type(data, { delay: 100 })
                .type("{enter}")
            cy.wait(1000)
            cy.get('.ag-row-odd > .ag-cell-value').eq(1).click()
        })
        cy.wait(1000)
        cy.deleteForm()
    }
    // verifyTheMaxLength(dataTable) {
    //     const fields = dataTable.hashes();

    //     // Log all fields for debugging
    //     cy.log('Fields:', JSON.stringify(fields));

    //     fields.forEach(row => {
    //         const fieldName = row.FieldName; // Ensure case matches the Gherkin table
    //         cy.log(`Processing field: ${fieldName}`);

    //         // Ensure fieldName is not undefined
    //         if (!fieldName) {
    //             throw new Error('FieldName is undefined. Please check the Gherkin table.');
    //         }

    //         // Verify maxlength
    //         cy.get(`[data-cy="${fieldName}"]`).click();
    //         cy.get('[data-cy="PicklistSearchBar"]')
    //             .invoke('attr', 'maxlength')
    //             .then((maxlength) => {
    //                 // Ensure maxlength exists
    //                 expect(maxlength).to.not.be.undefined;

    //                 // Log maxlength
    //                 cy.log(`${fieldName} maxlength is: ${maxlength}`);

    //                 // Test input behavior
    //                 const longString = 'A'.repeat(Number(maxlength) + 5);
    //                 cy.get('[data-cy="PicklistSearchBar"]').type(longString);
    //                 cy.get('[data-cy="PicklistSearchBar"]')
    //                     .invoke('val')
    //                     .should('have.length', Number(maxlength));
    //             });
    //     });
    // }

    verifyTheMaxLength(dataTable) {
        const fields = dataTable.hashes();

        fields.forEach(row => {
            const fieldNameWithAction = row.FieldName;

            cy.log(`Processing field: ${fieldNameWithAction}`);

            if (fieldNameWithAction.includes('PickListDrop')) {
                cy.wait(500)
                cy.get(`[data-cy="${fieldNameWithAction}"]`).should('be.visible').click();
                cy.wait(300)
                cy.get('[data-cy="PicklistSearchBar"]')
                    .invoke('attr', 'maxlength')
                    .then(maxlength => {
                        expect(maxlength).to.not.be.undefined;
                        const longString = 'A'.repeat(Number(maxlength) + 5);
                        cy.get('[data-cy="PicklistSearchBar"]').type(longString);
                        cy.get('[data-cy="PicklistSearchBar"]')
                            .invoke('val')
                            .should('have.length', Number(maxlength));
                        cy.get('.Picklistgridcloseicon > .mat-icon').click();
                    });
            } else if (fieldNameWithAction.includes('Input')) {
                cy.wait(500)
                cy.get(`[data-cy="${fieldNameWithAction}"]`).should('be.visible').focus().clear().invoke('attr', 'maxlength')
                    .then(maxlength => {
                        expect(maxlength).to.not.be.undefined;
                        const longString = '1'.repeat(Number(maxlength) + 5);
                        cy.get(`[data-cy="${fieldNameWithAction}"]`).type(longString)
                            .invoke('val').should('have.length', Number(maxlength));
                        cy.get(`[data-cy="${fieldNameWithAction}"]`).should('be.visible').clear()
                    });
            } else if (fieldNameWithAction.includes('Textarea')) {
                cy.wait(500)
                //cy.get('.FormOverView').scrollIntoView()
                cy.get(`[data-cy="${fieldNameWithAction}"]`).scrollIntoView().should('be.visible').clear().invoke('attr', 'maxlength')
                    .then(maxlength => {
                        expect(maxlength).to.not.be.undefined;
                        const longString = 'B'.repeat(Number(maxlength) + 5);
                        cy.get(`[data-cy="${fieldNameWithAction}"]`).type(longString)
                            .invoke('val').should('have.length', Number(maxlength));
                        cy.get(`[data-cy="${fieldNameWithAction}"]`).should('be.visible').clear()
                    });
            }
        });
    };
    verifyTheMaxLengthAndSave(dataTable) {
        const fields = dataTable.hashes();
        fields.forEach(row => {
            const fieldNameWithAction = row.FieldName

            cy.log(`Processing field: ${fieldNameWithAction}`);

            if (fieldNameWithAction.includes('PickListDrop')) {
                cy.wait(500)
                cy.get(`[data-cy="${fieldNameWithAction}"]`).should('be.visible').click();
                cy.wait(300)
                cy.get('[data-cy="PicklistSearchBar"]')
                    .invoke('attr', 'maxlength')
                    .then(maxlength => {
                        expect(maxlength).to.not.be.undefined;
                        const longString = 'A'.repeat(Number(maxlength) + 5);
                        //   const selectString = 'A'.repeat(Number(maxlength));
                        cy.get('[data-cy="PicklistSearchBar"]').type(longString);
                        cy.get('[data-cy="PicklistSearchBar"]')
                            .invoke('val')
                            .should('have.length', Number(maxlength));
                        cy.get('[data-cy="PicklistSearchBar"]')
                            .invoke('val').then((value) => {
                                cy.wrap(value).as('NewMaxlengthValue')
                                cy.get('.ag-center-cols-viewport').contains(`${value}(NEW)`).click();
                            })

                    })
            } else if (fieldNameWithAction.includes('Textarea')) {
                cy.wait(500)
                //cy.get('.FormOverView').scrollIntoView()
                cy.get(`[data-cy="${fieldNameWithAction}"]`).scrollIntoView().should('be.visible').clear().invoke('attr', 'maxlength')
                    .then(maxlength => {
                        expect(maxlength).to.not.be.undefined;
                        const longString = 'B'.repeat(Number(maxlength) + 5);
                        cy.get(`[data-cy="${fieldNameWithAction}"]`).type(longString)
                            .invoke('val').should('have.length', Number(maxlength));

                    });
            }
        })
    }
    deleteSaveGCMFormWithMaxLengthValues(element) {
        cy.get(`[data-cy="${element}-PickListDrop"]`).should('be.visible').click();
        cy.get('@NewMaxlengthValue').then((val) => {
            cy.get('[data-cy="PicklistSearchBar"]').type(val)
            cy.get('[data-cy="PicklistGrid"]').contains(val).click()
            cy.wait(1000)
        })
    }
    VerifyFormValidationMessage(Page) {
        cy.wait(1000); // Small wait to ensure any popups appear

        const pageMapping = {
            'Instrument Master': 'InstrumentMasterErrorMessages',
            'Skill Master': 'SkillMasterErrorMessages',
            'Measure Options Type': 'MeasureOptionsTypeErrorMessages',
            'Subject Details': 'SubjectDetailsErrorMessages',
            'DAPoint Group': 'DAPointGroupErrorMessages',
            'Key Result Area': 'KeyResultAreaErrorMessages',
            'Lead Cycle': 'LeadCycleErrorMessages',
            'Shift Master': 'ShiftMasterErrorMessages',
            'Shift Master Full': 'ShiftMasterFullErrorMessages',
            'Standard': 'StandardErrorMessages',
            'Counter': 'CounterErrorMessages',
            'NOC List': 'NOCListErrorMessages',
            'Common': 'CommonErrorMessages',
            'Terms': 'TermsErrorMessages',
            'Machine': 'MachineErrorMessages',
            'PackSet': 'PackSetErrorMessages',
            'Pack': 'PackErrorMessages',
            'BankBranch': 'BankBranchErrorMessages',
            'Activity': 'ActivityErrorMessages',
            'Company': 'CompanyErrorMessages',
            'Allocation Type': 'AllocationTypeErrorMessages',
            'Process Master': 'ProcessMasterErrorMessages',
            'Asset Type': 'AssetTypeErrorMessages',
            'Target Master': 'TargetMasterErrorMessages',
            'Cost Center Pattern': 'CostCenterPatternErrorMessages',
            'Department': 'DepartmentErrorMessages',
            'Cost Analysis': 'CostAnalysisErrorMessages',
            'Cost Center Type': 'CostCenterTypeErrorMessages',
            'Resource Type': 'ResourceTypeErrorMessages',
            'Driver': 'DriverErrorMessages',
            'PartyTaxType': 'PartyTaxTypeErrorMessages',
            'DB Join': 'DBJoinTypeErrorMessages',
            'Page': 'PageErrorMessages',
            'Division': 'DivisionErrorMessages',
            'Item Sub Category': 'ItemSubCategoryErrorMessages',
            'Model': 'ModelErrorMessages',
            'Count Master': 'CountMasterErrorMessages',
            'Process Operation': 'ProcessOperationErrorMessages',
            'Sinle Visitor Pass': 'SingleVisitorPassErrorMessages',
            'Program Assembly': 'ProgramAssemblyErrorMessages',
            'Formula Field': 'FormulaFieldErrorMessages',
            'Inspection Area': 'InspectionAreaErrorMessages',
            'Account Master': 'AccountMasterErrorMessages',
            'Device': 'DeviceErrorMessages',
            'UOM': 'UOMErrorMessages',
            'Currency': 'CurrencyErrorMessages',
            'Leave Type': 'LeaveTypeErrorMessages',
            'Cycle Count Class': 'CycleCountClassErrorMessages',
            'Insurance': 'InsuranceMasterErrorMessages',
            'City': 'CityErrorMessages',
            'Goal': 'GoalErrorMessages',
            'Bin': 'BinErrorMessages',
            'User Group': 'UserGroupErrorMessages',
            'Distribution List': 'DistributionListErrorMessages',
            'Cost Category': 'CostCategoryErrorMessages',
            'Cost Center': 'CostCenterErrorMessages',
            'Analysis': 'AnalysisErrorMessages',
            'Measure Options': 'MeasureOptionsErrorMessages',
            'Document Type': 'DocumentTypsErrorMessages',
            'Security Group': 'SecurityGroupErrorMessages',
            'Module': 'ModuleErrorMessages',
            'MRP Party Type': 'MRPPartyTypeErrorMessages',
            'SKU Setting': 'SKUSettingErrorMessages',
            'Shift Pattern': 'ShiftPatternErrorMessages',
            'Caste Category': 'CasteCategoryErrorMessages',
            'Gratuity Settings': 'GratuitySettingsErrorMessages',
            'Man Power Requirement Plan': 'ManPowerRequirementPlanErrorMessages',
            'Selection Cycle': 'SelectionCycleErrorMessages',
            'Performance Measure': 'PerformanceMeasureErrorMessages',
            'Employee Nominee': 'EmployeeNomineeErrorMessages',
            'Forecast': 'ForecastErrorMessages',
            'UOM SET': 'UOMSETErrorMessages',
            'ActivityRecruitmentProcess': 'ActivityRecruitmentProcessErrorMessages',
            'Parameter': 'ParameterErrorMessages',
            'organization Group': 'organizationGroupErrorMessages',
            'vehicle': 'vehicleErrorMessages',
            'Contact': 'ContactErrorMessages',
            'GCM Type': 'GCMTypeErrorMessages',
            'BIZ Transaction': 'BizTransactionErrorMessages',
            'Setting Costing': 'SettingCostingErrorMessages',
            'Branch': 'BranchErrorMessages',
            'AccountSchedule': 'AccountScheduleErrorMessages',
            'BIZ Transaction Sub Class': 'BizTransactionSubClassErrorMessages',
            'Asset Type Material': 'AssetTypeMaterialErrorMessages',
            'ForecastSet': 'ForecastSetErrorMessages',
            'Event Type': 'EventTypeErrorMessages',
            'MachineType': 'MachineTypeErrorMessages',
            'Settings': 'SettingsErrorMessages',
            'TDSCategory': 'TDSCategoryErrorMessages',
            'DocumentSet': 'DocumentSetErrorMessages',
            'EventClass': 'EventClassErrorMessages',
            'Mail List': 'MailListErrorMessages',
            'MRP': 'MRPErrorMessages',
            'Employee Vs Qualification': 'EmployeeVsQualificationErrorMessages',
            'WorkFlowRule': 'WorkFlowRuleErrorMessages',
            'Employee Reference Master': 'EmployeeReferenceMasterErrorMessages',
            'ProblemVsSolution': 'ProblemVsSolutionErrorMessages',
            'Store': 'StoreErrorMessages',
            'Charge Element': 'ChargeElementErrorMessages',
            'WorkCenter': 'WorkCenterErrorMessages',
            'ECM Rights': 'ECMRightsErrorMessages',
            'Document Map': 'DocumentMapErrorMessages',
            'Pay Period': 'PayPeriodErrorMessages',
            'Contract Work Details Entry': 'ContractWorkDetailsEntryErrorMessages',
            'Period Update': 'PeriodUpdateErrorMessages',
            'Day Update': 'DayUpdateErrorMessages',
            'Price List Type': 'PriceListTypeErrorMessages',
            'Cash Discount': 'CashDiscountErrorMessages',
            'OU Level Setting': 'OULevelSettingErrorMessages',
            'Employee Master': 'EmployeeMasterErrorMessages',
            'Employee Working History': 'EmployeeWorkingHistoryErrorMessages',
            'Employee Vs Experience': 'EmployeeVsExperienceErrorMessages',
            'Employee Salary History': 'EmployeeSalaryHistoryErrorMessages',
            'Leave Credit Process': 'LeaveCreditProcessErrorMessages',
            'Undo Payroll Processing': 'UndoPayrollProcessingErrorMessages',
            'Perquisites12BA': 'Perquisites12BAErrorMessages',
            'Leave Debit Process': 'LeaveDebitProcessErrorMessages',
            'Leave Request Bulk': 'LeaveRequestBulkErrorMessages',
            'Other Earnings Detail': 'OtherEarningsDetailErrorMessages',
            'EMI Reschedule': 'EMIRescheduleErrorMessages',
            'Punch Add Edit Screen': 'PunchAddEditScreenErrorMessages',
            'Advance Payment': 'AdvancePaymentErrorMessages',
            'Target Share': 'TargetShareErrorMessages',
            'LOV': 'LovErrorMessages',
            'Security Question': 'SecurityQuestionErrorMessages',
            'Item SKU': 'ItemSKUErrorMessages',
            'Asset Activity': 'AssetActivityErrorMessages',
            'Delivery': 'DeliveryErrorMessages',
            'Resource': 'ResourceErrorMessages',
            'Problem': 'ProblemErrorMessages',
            'Die Master': 'DieMasterErrorMessages',
            'Advance Request': 'AdvanceRequestErrorMessages',
            'BIZ Transaction Key': 'BIZTransactionKeyErrorMessages',
            'Item Category': 'ItemCategoryErrorMessages',
            'Separation': 'SeparationErrorMessages',
            'Employee Personal Details': 'EmployeePersonalDetailsErrorMessages',
            'Lot Type': 'LotTypeErrorMessages',
            'Party Price Category': 'PartyPriceCategoryErrorMessages',
            'Deferral Plan Master': 'DeferralPlanMasterErrorMessages',
            'Mail Setting': 'MailSettingErrorMessages',
            'Apply Advance': 'ApplyAdvanceErrorMessages',
            'Apply Leave': 'ApplyLeaveErrorMessages',
            'Employee Skill': 'EmployeeSkillErrorMessages',
            'Advance Repayment': 'AdvanceRepaymentErrorMessages',
            'OULevel': 'OULevelErrorMessages',
            'IT Declaration': 'ITDeclarationErrorMessages',
            'Attendance Adjustment Entry': 'AttendanceAdjustmentEntryErrorMessages',
            'Holiday Update': 'HolidayUpdateErrorMessages',
            'State': 'StateErrorMessages',
            'OrganizationUnit': 'OrganizationUnitErrorMessages',
            'PaymentTerm': 'PaymentTermErrorMessages',
            'User': 'UserErrorMessages',
            'TDS Calculate': 'TDSCalculateErrorMessages',
            'TDS Challan': 'TDSChallanErrorMessages',
            'Group Master': 'GroupMasterErrorMessages',
            'Narration Master': 'NarrationMasterErrorMessages',
            'Skill Process Stage Map': 'SkillProcessStageMapErrorMessages'

        };

        // Ensure the page name is mapped correctly
        if (!pageMapping[Page]) {
            throw new Error(`Page "${Page}" is not found in the mapping.`);
        }

        cy.fixture('validation-error-message.json').then((errorMessageJson) => {
            // Ensure the error message structure exists
            if (!errorMessageJson[pageMapping[Page]]) {
                throw new Error(`Error message mapping for "${Page}" not found in fixture.`);
            }

            const expectedErrorMessages = errorMessageJson[pageMapping[Page]].ErrorMessages;

            // Ensure the dialog appears before interacting with it
            cy.get('body').then($body => {
                if ($body.find('.mat-mdc-dialog-component-host').length > 0) {
                    cy.get('.mat-mdc-dialog-component-host').should('be.visible').then($dialog => {
                        const actualText = $dialog.text();
                        const errorExists = expectedErrorMessages.some(message => actualText.includes(message));

                        // Add Cypress assertion for better debugging
                        expect(errorExists, `Expected one of ${expectedErrorMessages}, but found: "${actualText}"`).to.be.true;
                        cy.log(`Validation error found: ${errorExists}`);
                    });
                } else {
                    cy.log('No validation dialog appeared.');
                    expect(false, 'Expected validation dialog but none appeared').to.be.true;
                }
            });
        });
    }

    // VerifyFormValidationMessage(){
    //     const pageMapping={
    //         'test':'testErrorPage'
    //     }
    //     let errorMessageJson;
    //     cy.fixture('errorMessages.json').then((data)=>{
    //         errorMessageJson=data;
    //     })
    //     cy.get('message box xpath or class or id').then($body=>)
    // }

    // enterCodeValue(value, field) {
    //     cy.get(`[data-cy="${field}-PickListDrop"]`)
    //         .scrollIntoView()
    //         .should('be.visible')
    //         .click(); // Click directly if visible

    //     cy.get('[data-cy="PicklistSearchBar"]')
    //         .should('be.visible')
    //         .clear()
    //         .type(value); // Enter search value

    //     cy.wait(1000); // Optional, but may help if the search needs time

    //     cy.get('.ag-center-cols-viewport')
    //         .contains(`${value}(NEW)`)
    //         .should('be.visible')
    //         .click(); // Select the value     

    //     cy.setData(field, value); // Store value

    //     cy.getData(field).then((data) => {
    //         cy.log(`Selected Value: ${data}`); // Ensure proper logging
    //     });

    //     cy.wait(2000); // Optional, but avoid excessive waiting
    // }

    enterCodeValue(value, field) {
        if (!value) {
            // Skip all actions if value is empty
            return;
        }
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
                    cy.get('div.alertbutton').click();
                    cy.get('.mat-mdc-dialog-component-host > .mat-icon').click();
                    

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
    }

    enterNameValue(value, field) {
        if (!value) {
            // Skip all actions if value is empty
            return;
        }
        cy.get(`[data-cy="${field}-PickListDrop"]`).click()
        cy.get('[data-cy="PicklistSearchBar"]').should('be.visible').clear().type(value);
        cy.wait(1000);
        cy.get('.ag-center-cols-viewport').contains(`${value}(NEW)`).click();
        cy.setData(`${field}`, value)
        cy.getData(`${field}`).then((data) => {
            cy.log(data)
            cy.wait(2000);

        })
    }
}





// afterEach moved to cypress/support/e2e.js where lifecycle hooks belong
