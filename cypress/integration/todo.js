context("ToDo", () => {
  before(() => {
    cy.login("Administrator", "admin");
    cy.visit("/desk");
  });

  it("creates a new todo", () => {
    cy.visit("/app/todo/new");
    cy.get_field("description", "Text Editor")
      .type("this is a test todo", { force: true })
      .wait(400);
    cy.get(".page-title").should("contain", "Not Saved");
    cy.intercept({
      method: "POST",
      url: "api/method/frappe.desk.form.save.savedocs",
    }).as("form_save");
    cy.get(".primary-action").click();
    cy.wait("@form_save").its("response.statusCode").should("eq", 200);

    cy.go_to_list("ToDo");
    cy.clear_filters();
    cy.get(".page-head").findByTitle("To Do").should("exist");
    cy.get(".list-row").should("contain", "this is a test todo");
  });
});
