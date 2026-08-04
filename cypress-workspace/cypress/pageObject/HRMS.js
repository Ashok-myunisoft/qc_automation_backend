
import { calculateShiftMinutes } from "./Utils";

export class HRMS {

    CreateMeasureOptionTypeScreen() {
        cy.get('@inputData').then((data) => {
            const value = data[1].MeasureOptionType;
            cy.handleEntityDetails('LovTypeCode', 'LovTypeName', value.code, value.name)

            cy.clickRadioField('LovTypeScoreRequired', 'No')
            cy.clickRadioField('LovTypeLovNature', 'Dependent')
            cy.clickRadioField('LovTypeGenerationType', 'Auto')

            cy.selectValueInPickListDrop('ParentLovTypeName', value.parent)

            cy.saveForm()
        })
    }
    CreateSubjectDetailsScreen() {
        cy.get('@inputData').then((data) => {
            const value = data[2].SubjectDetails;
            cy.handleEntityDetails('SubjectCode', 'SubjectName', value.code, value.name)

            cy.enterTextAreaValue('SubjectParticulars', value.SDremarks)

            cy.saveForm()
        })
    }
    CreateKRA() {
        cy.get('@inputData').then((data) => {
            const value = data[4].KeyResultArea;
            cy.handleSingleField('KRAName', value.name)
            cy.selectValueInPickListDrop('DepartmentName', value.department)
            cy.enterTextAreaValue('KRADescription', value.description)
            cy.enterTextAreaValue('KRARemarks', value.description)
            cy.enterInputValue('KRASection', value.section)

        })
    }
    CreateShiftMasterScreen() {
        cy.get('@inputData').then((data) => {
            const value = data[5].shiftMaster;
            cy.handleEntityDetails('ShiftCode', 'ShiftDescription', value.code, value.description)

            cy.clickCheckBox('ShiftIsProductionShift')
            cy.setTimeInput('TotalShiftStartTime', value.startTime)
            cy.setTimeInput('TotalShiftEndTime', value.endTime)

            const expectedMinutes = calculateShiftMinutes(value.startTime, value.endTime);
            cy.get('[data-cy="ShiftShiftMinutes-Input"]').should('have.value', expectedMinutes.toString());

            cy.setTimeInput('FirstHalfEndTimeExtraField', value.firstHalfEndTime)

            cy.enterInputValue(value.graceMinutes, 'ShiftGraceMinutes')

            cy.clickCheckBox('ShiftIsBreakApplicable')

            cy.enterInputValue(value.breakMinutes, 'ShiftBreakMinutes')


            cy.enterInputValue(value.firstHalfCutOffMinutes, 'ShiftCutOffMinutes')
            cy.enterInputValue(value.secondHalfCutOffminutes, 'ShiftCutOffMinutesOut')


            cy.enterInputValue(value.fullDayMinutes, 'ShiftFullDayMinutes')

            cy.enterInputValue(value.halfDayMinutes, 'ShiftHalfDayMinutes')


            cy.selectAndVerifyComboBox('Shift Based', 'ShiftWorkTimeMethod')
            cy.enterTextAreaValue('ShiftRemarks', value.SMremarks)

        })
    }
    CreateShiftMaster(Rowindex, Conditcion) {

        cy.get('@inputData').then((data) => {

            const value = data[Rowindex][Conditcion];

            if (value.code || value.description) {
                cy.handleEntityDetails('ShiftCode', 'ShiftDescription', value.code, value.description)
            }
           if(value.ShiftIsProductionCheckBox){
            cy.clickCheckBox(value.ShiftIsProductionCheckBox)
           }
            if (value.startTime) {
                cy.setTimeInput('TotalShiftStartTime', value.startTime)
            }
            if (value.startTime) {
                cy.setTimeInput('TotalShiftEndTime', value.endTime)
                const expectedMinutes = calculateShiftMinutes(value.startTime, value.endTime);
                cy.get('[data-cy="ShiftShiftMinutes-Input"]').should('have.value', expectedMinutes.toString());
            }
            if (value.firstHalfEndTime) {
                cy.setTimeInput('FirstHalfEndTimeExtraField', value.firstHalfEndTime)
            }
            if (value.graceMinutes) {
                cy.enterInputValue(value.graceMinutes, 'ShiftGraceMinutes')
            }
            if (value.BreakApplicablecheckbox) {
            cy.clickCheckBox(value.BreakApplicablecheckbox)
            }
            if (value.breakMinutes) {
                cy.enterInputValue(value.breakMinutes, 'ShiftBreakMinutes')
            }
            if (value.firstHalfCutOffMinutes) {
                cy.enterInputValue(value.firstHalfCutOffMinutes, 'ShiftCutOffMinutes')
            }
            if (value.secondHalfCutOffminutes) {
                cy.enterInputValue(value.secondHalfCutOffminutes, 'ShiftCutOffMinutesOut')
            }
            if (value.fullDayMinutes) {
                cy.enterInputValue(value.fullDayMinutes, 'ShiftFullDayMinutes')
            }
            if (value.halfDayMinutes) {
                cy.enterInputValue(value.halfDayMinutes, 'ShiftHalfDayMinutes')
            }
            if (value.PunchType) {
            cy.selectAndVerifyComboBox(value.PunchType, 'ShiftWorkTimeMethod')
            }
            if (value.SMremarks) {
                cy.enterTextAreaValue('ShiftRemarks', value.SMremarks)
            }
        })
    }
}