# Prompt for Codex

You are an experienced Senior Software Engineer and Technical Writer.

Your task is to thoroughly analyze the entire Visual Studio Code workspace and produce a comprehensive, high-quality `README.md`.

## Objectives

1. **Understand the project before writing anything**

   * Inspect the complete repository.
   * Read the source code, folder structure, configuration files, scripts, documentation, and comments.
   * Infer the project's purpose, architecture, and workflow from the implementation.
   * Do not rely only on the existing README.

2. **Rewrite the README from scratch if necessary**

   * Keep useful existing information, but improve and reorganize it.
   * Remove outdated or misleading content.
   * Fill in missing sections based on the actual implementation.

3. **Start with an intuitive project explanation**
   The first section must explain, in simple and accessible language:

   * What this project is.
   * What problem it solves.
   * Why someone would use it.
   * Who the intended users are.
   * What makes it different or interesting.

   Assume the reader has never seen this repository before.

   Avoid technical jargon in this introduction whenever possible.

4. **After the introduction, provide a detailed technical description**

   Include as many relevant sections as appropriate, such as:

   * Project Overview
   * Features
   * Architecture
   * Repository Structure
   * Main Components
   * Technologies Used
   * Dependencies
   * Build Instructions
   * Installation
   * Configuration
   * Usage
   * Execution Flow
   * Data Flow
   * APIs
   * Modules
   * Libraries
   * Configuration Files
   * Environment Variables
   * Logging
   * Testing
   * Performance Considerations
   * Limitations
   * Future Improvements
   * Troubleshooting
   * FAQ (if appropriate)

5. **Explain the architecture**

   Describe:

   * how the modules interact;
   * how data flows through the application;
   * the responsibility of each major component;
   * why the architecture is organized this way.

6. **Document every important folder**

   Explain the purpose of each significant directory and the files inside it.

7. **Explain the codebase**

   Identify:

   * entry points;
   * main classes;
   * important functions;
   * design patterns;
   * algorithms;
   * communication between components.

8. **Document setup and execution**

   Explain exactly:

   * prerequisites;
   * installation;
   * dependency installation;
   * build commands;
   * execution commands;
   * testing commands;
   * development workflow.

9. **Create diagrams using Mermaid**

   Whenever useful, include diagrams such as:

   * System Architecture
   * Component Diagram
   * Sequence Diagram
   * Workflow Diagram
   * Data Flow Diagram

10. **Provide examples**

    Include examples for:

    * installation;
    * configuration;
    * execution;
    * expected outputs;
    * common use cases.

11. **Be accurate**

    Never invent functionality.

    If something cannot be inferred from the repository, explicitly state:

    > "This could not be determined from the current codebase."

12. **Improve readability**

    Use:

    * clear headings;
    * tables where appropriate;
    * bullet lists;
    * code blocks;
    * diagrams;
    * concise explanations.

13. **Target audience**

    The README should be understandable by:

    * new developers;
    * contributors;
    * technical managers;
    * users evaluating the project.

14. **Output**

    Produce a polished, professional `README.md` that could serve as the official documentation of the project.

The final README should be self-contained, easy to navigate, technically accurate, and significantly more complete than the current version.