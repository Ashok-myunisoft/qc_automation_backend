// REFERENCE definitions in the house style (taken from a script that ran on the Skill Domain screen).
// The BEHAVIOR object is READ FROM THE SCREEN'S HTML/TS facts; never copy these values to another screen.
const BEHAVIOR = {
  roles: { reset: { title: 'New', class: 'add' }, close: { title: '', class: 'closeicon' }, delete: { title: 'Delete', class: 'delete' } },
  modal: { selector: '.modal' },
  list: {
    filter: { class: 'filter-select', all_option: 'All Types' },
    sortable_columns: [{ field: 'DomainCode', label: 'Code' }, { field: 'IsActive', label: 'Status' }],
    views: [{ mode: 'list', title: 'List View' }, { mode: 'card', title: 'Card View' }],
    list_container: 'table-container', card_container: 'card-grid',
    counter_label_class: 'stat-label', counter_number_class: 'stat-number',
    empty_state: { class: 'empty-state', message: 'No Domains Found' },
    sort_ranks: { IsActive: { ACTIVE: 0, INACTIVE: 1 } }
  }
};
const LIST = BEHAVIOR.list;
const MODAL_SEL = BEHAVIOR.modal.selector;
const dotted = (c) => (c ? '.' + String(c).trim().split(/\s+/)[0] : '');
const aliasOf = (label) => 'counter_' + String(label).replace(/\W+/g, '_');
const formVisible = ($b) => $b.find(MODAL_SEL).filter(':visible').length;
function listRoot($b) {
  const sel = [dotted(LIST.list_container), dotted(LIST.card_container)].filter(Boolean).join(', ');
  const $r = sel ? $b.find(sel).filter(':visible') : $b;
  return $r.length ? $r : $b;
}
function clickFirstVisible(label, selectors) {
  cy.get('body', { timeout: 30000 }).should(($b) => {
    expect(selectors.some((s) => $b.find(s).filter(':visible').length > 0), `a visible "${label}" control`).to.equal(true);
  });
  cy.get('body').then(($b) => {
    const sel = selectors.find((s) => $b.find(s).filter(':visible').length > 0);
    cy.wrap($b.find(sel).filter(':visible').first()).scrollIntoView().click({ force: true });
  });
  cy.wait(1000);
}

// a key field of a NEW record that is a picklist button: open it, type the new value in its search bar
Then("I enter {string} in the {string} new record field", (value, field) => {
  if (!value) return;
  const known = CONTROLS[field];
  const names = [known && known.dataCy, `${field}-Input`, `${field}-PickList`, `${field}-PickListDrop`].filter(Boolean);
  const found = ($b) => names.find((n) => $b.find(`[data-cy="${n}"]`).filter(':visible').length > 0);
  cy.get('body', { timeout: 30000 }).should(($b) => { expect(found($b), `the "${field}" control`).to.be.a('string'); });
  cy.get('body').then(($b) => {
    const $el = $b.find(`[data-cy="${found($b)}"]`).filter(':visible').first();
    const $input = $el.is('input, textarea') ? $el : $el.find('input, textarea').filter(':visible').first();
    if ($input.length) {
      cy.wrap($input).scrollIntoView().click({ force: true }).clear({ force: true }).type(value, { force: true, delay: 50 });
      return;
    }
    cy.wrap($el).click({ force: true });
    cy.get("[data-cy='PicklistSearchBar']").should('be.visible').click().clear().type(value);
    cy.wait(1500);
    cy.get('body').then(($b2) => {
      const $new = $b2.find('[data-cy*="AddNew"], .addnew, [class*="add-new"]').filter(':visible').first();
      if ($new.length) cy.wrap($new).click({ force: true });
    });
  });
});

Then("the form should be open", () => {
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(formVisible($b), 'a visible form').to.be.greaterThan(0); });
});
Then("the form should be closed", () => {
  cy.wait(800);
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(formVisible($b), 'visible forms').to.equal(0); });
});

Then("the {string} action should be {string}", (title, state) => {
  const re = new RegExp('^\\s*' + String(title).replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*$', 'i');
  cy.get('body', { timeout: 10000 }).should(($b) => {
    const $scope = $b.find(MODAL_SEL).filter(':visible').first();
    expect($scope.length, 'an open form').to.be.greaterThan(0);
    const $hit = $scope.find('*').filter(':visible').filter((i, e) => {
      const t = String(e.getAttribute('title') || e.getAttribute('aria-label') || '').trim();
      return re.test(t) || (e.children.length === 0 && re.test(Cypress.$(e).text()));
    });
    if (/^visible$/i.test(state)) expect($hit.length, `the "${title}" action is visible`).to.be.greaterThan(0);
    else expect($hit.length, `the "${title}" action is hidden`).to.equal(0);
  });
});

Then("I click the Reset button", () => {
  const r = BEHAVIOR.roles.reset;
  clickFirstVisible('Reset', [`${MODAL_SEL} ${dotted(r.class)}`, `${MODAL_SEL} [title="${r.title}"]`, '[data-cy="ResetForm"]']);
});

Then("I search the list for {string}", (value) => {
  cy.get('body', { timeout: 15000 }).then(($b) => {
    const $box = findSearchBox($b);
    expect($box.length, 'a search box').to.be.greaterThan(0);
    cy.wrap($box).click({ force: true }).clear({ force: true }).type(value, { force: true, delay: 30 });
    cy.wait(1200);
  });
});

Then("I should see the record {string} in the list", (value) => {
  cy.get('body', { timeout: 15000 }).should(($b) => {
    expect(squashText(listRoot($b).text()).includes(squashText(value)), `"${value}" is in the list`).to.equal(true);
  });
});

Then("the list row {string} should show {string}", (record, text) => {
  cy.get('body', { timeout: 15000 }).should(($b) => {
    const $leaf = listRoot($b).find('*').filter(':visible')
      .filter((i, e) => e.children.length === 0 && squashText(e.textContent).includes(record)).first();
    expect($leaf.length, `a list entry for "${record}"`).to.be.greaterThan(0);
    let $row = $leaf.closest('tr');
    if (!$row.length) $row = $leaf.closest('[class*="card"]:not([class*="grid"]):not([class*="header"]):not([class*="body"])');
    expect($row.length, `the row of "${record}"`).to.be.greaterThan(0);
    expect(squashText($row.text()).toLowerCase()).to.include(squashText(text).toLowerCase());
  });
});

Then("I filter the list by type {string}", (value) => {
  cy.get(`select${dotted(LIST.filter.class)}`, { timeout: 15000 }).filter(':visible').first().select(value);
  cy.wait(800);
});

Then("I sort the list by the {string} column in {string} order", (label, order) => {
  const clicks = /^asc/i.test(order) ? 1 : 2;       // a column starts ascending; a second click reverses it
  for (let i = 0; i < clicks; i += 1) {
    cy.get('th', { timeout: 15000 }).filter(':visible')
      .filter((j, e) => squashText(e.textContent).toLowerCase() === String(label).toLowerCase()).first().click({ force: true });
    cy.wait(400);
  }
});

Then("I switch to the {string} view", (view) => {
  const v = LIST.views.find((x) => x.mode === (/card/i.test(view) ? 'card' : 'list'));
  cy.get(`[title="${v.title}"]`, { timeout: 15000 }).filter(':visible').first().click({ force: true });
  cy.wait(500);
});

Then("I should see the empty state {string}", (message) => {
  const sel = dotted(LIST.empty_state.class);
  cy.get(sel, { timeout: 15000 }).filter(':visible').should('have.length.greaterThan', 0);
  cy.get(sel).filter(':visible').first().should('contain.text', message);
});

function counterValue($b, label) {
  const $l = $b.find('.' + LIST.counter_label_class)
    .filter((i, e) => squashText(e.textContent).toLowerCase() === String(label).toLowerCase()).first();
  return parseInt(squashText($l.parent().find('.' + LIST.counter_number_class).first().text()), 10);
}
Then("I remember the number shown for {string}", (label) => {
  cy.get('body', { timeout: 15000 }).should(($b) => { expect(Number.isNaN(counterValue($b, label))).to.equal(false); });
  cy.get('body').then(($b) => { cy.wrap(counterValue($b, label)).as(aliasOf(label)); });
});
Then("the number shown for {string} should have changed by {string}", (label, delta) => {
  cy.get('@' + aliasOf(label)).then((before) => {
    cy.get('body', { timeout: 15000 }).should(($b) => {
      expect(counterValue($b, label), `"${label}" changed by ${delta}`).to.equal(before + parseInt(delta, 10));
    });
  });
});