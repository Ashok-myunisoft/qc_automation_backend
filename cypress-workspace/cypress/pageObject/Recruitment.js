export class Recruitment {

    fillBasicDetails(data) {
        cy.selectValueInPickListDrop(data.jobProfile, 'JobProfileName')
        cy.enterInputValue(data.jobTitle, 'JobTitle')
        cy.selectValueInPickListDrop(data.department, 'DepartmentName')
        cy.selectValueInPickListDrop(data.location, 'PrimaryLocationName')
        cy.selectValueInPickListDrop(data.experience, 'ExperianceName')
        cy.selectValueInPickListDrop(data.designation, 'DesignationName')
    }

    fillJobDescription(data) {
        cy.enterTextAreaValue('JobDescription', data.description)
        cy.enterTextAreaValue('JobResponsibilities', data.keyResponsibilities)
        cy.enterTextAreaValue('JobRequirements', data.requirements)
    }

    fillContacts(data) {
        cy.selectValueInPickListDrop(data.requestor, 'ReportingToEmployeeName')
        cy.selectValueInPickListDrop(data.hiringManager, 'HiringManagerName')
        cy.selectValueInPickListDrop(data.hrIncharge, 'HRInchargeName')
    }

    fillPublishingSettings(data) {
        cy.selectAndVerifyComboBox(data.status, 'JobJobStatus')
        cy.setDate(data.startDate, 'JobRecruitmentStartDate')
        cy.setDate(data.endDate, 'JobRecruitmentEndDate')
    }

    clickWizardNextStep() {
        cy.contains('button', 'Next Step').click()
    }

    clickCreateJobOpening() {
        cy.contains('button', 'Create Job Opening').click()
    }

    switchToFormView() {
        cy.contains('button, .toggle-option', 'Form').click()
    }

    // ─── Job Profile Library ───────────────────────────────────────────────────

    fillJobProfileBasicInfo(data) {
        cy.selectAndVerifyComboBox(data.jobProfile, 'JobProfileName')
        cy.selectValueInPickListDrop(data.department, 'JobProfileDepartmentName')
        cy.selectValueInPickListDrop(data.title, 'JobProfileTitleName')
        if (data.summary) cy.enterTextAreaValue('JobProfileSummary', data.summary)
        if (data.description) cy.enterTextAreaValue('JobProfileDescription', data.description)
    }

    addResponsibilityRow(data, rowIndex = 0) {
        cy.get('[data-cy="AddRow"]').first().click()
        cy.selectValueInPickListDrop(data.domain, `JobProfileDutyDomainName${rowIndex}`)
        cy.selectValueInPickListDrop(data.section, `JobProfileDutySectionName${rowIndex}`)
        cy.enterInputValue(data.particulars, `JobProfileDutyParticulars${rowIndex}`)
    }

    addKRARow(data, rowIndex = 0) {
        cy.get('[data-cy="AddRow"]').first().click()
        cy.selectValueInPickListDrop(data.domain, `JobProfileKRADomainName${rowIndex}`)
        cy.selectValueInPickListDrop(data.section, `JobProfileKRASectionName${rowIndex}`)
        cy.selectValueInPickListDrop(data.objective, `JobProfileKRAObjectiveName${rowIndex}`)
        cy.selectValueInPickListDrop(data.kraName, `JobProfileKRAKRAName${rowIndex}`)
        if (data.particulars) cy.enterInputValue(data.particulars, `JobProfileKRAParticulars${rowIndex}`)
    }

    addRequirementRow(data, rowIndex = 0) {
        cy.get('[data-cy="AddRow"]').last().click()
        cy.selectValueInPickListDrop(data.domain, `JobProfileRequirementDomainName${rowIndex}`)
        cy.selectValueInPickListDrop(data.section, `JobProfileRequirementSectionName${rowIndex}`)
        if (data.particulars) cy.enterInputValue(data.particulars, `JobProfileRequirementParticulars${rowIndex}`)
    }

    addSkillRow(data, rowIndex = 0) {
        cy.get('[data-cy="AddRow"]').first().click()
        cy.selectValueInPickListDrop(data.domain, `JobProfileSkillDomainName${rowIndex}`)
        cy.selectValueInPickListDrop(data.section, `JobProfileSkillSectionName${rowIndex}`)
        cy.selectValueInPickListDrop(data.skillName, `JobProfileSkillSkillName${rowIndex}`)
        if (data.particulars) cy.enterInputValue(data.particulars, `JobProfileSkillParticulars${rowIndex}`)
    }
}
