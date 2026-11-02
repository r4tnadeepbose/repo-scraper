# repo-scraper

## About

`repo-scraper` is a quiet way to understand the work behind a public GitHub
profile. I made it to understand how a particular person is working, what
they are building, how their projects are growing, which aspects of code they
write, and what they have been working on over time.

It brings those public signals together into one clear overview: projects,
languages, tools, README notes, repository structure, and visible activity.
The result is useful for understanding a developer's public portfolio,
following technical growth, comparing project directions, or preparing for a
technical conversation.

The tool is intentionally focused on public repository information. It is not
intended to identify, target, manipulate, impersonate, or socially engineer
people.

Enter a GitHub username or organization and the tool collects:

- Repository names and links
- README files
- File trees
- Languages
- Stars and forks
- Topics, licenses, branches, and update dates
- File counts, file types, and repository size estimates

It is useful for reviewing your own projects, preparing a portfolio overview,
or doing respectful team and developer research from publicly shared work.

## Screenshots

Screenshots can be added here when they are available:

```text
screenshots/
├── dashboard.png
├── repository-details.png
└── html-report.png
```

The application includes a dark repository dashboard, searchable repository
details, README rendering, file-tree browsing, and a standalone HTML report.

## Features

- Dark desktop interface built with Tkinter
- Search and filter repositories
- Sort by name, language, file count, or size
- Select repositories with checkbox controls
- View rendered README content
- Browse collapsible file trees
- View repository statistics
- Copy repository URLs, README text, Markdown, or full text dumps
- Export JSON, Markdown, and self-contained HTML reports
- Automatically open the HTML report after a scan, with an opt-out checkbox
- Copy all repositories or copy one complete repository from the HTML report
- Hide forked or archived repositories
- Optional GitHub token support for higher API limits
- Cancel an active scrape

## Requirements

- Python 3.9 or newer
- Internet access
- No third-party Python packages are required

## Run

From the project directory:

```powershell
python .\main.py
```

Then enter a GitHub username or organization and press **Enter** or click
**SCRAPE**.

## Optional GitHub token

Without a token, GitHub's API has a lower request limit. You can paste a
personal access token into the token field while the application is running.

The token is used only for the current session and is not saved to the
configuration file.

For normal repository metadata, the token does not need write permissions.

## Exports

The application can export:

- **JSON** for structured data and further processing
- **Markdown** for readable notes or documentation
- **HTML** for a searchable standalone report
- **Text clipboard dumps** for quick sharing

Reports contain information retrieved from public GitHub repositories. README
content and file paths are included, but source file contents are not
downloaded.

## Responsible use

Use this tool only with publicly available information and respect GitHub's
terms, rate limits, and the privacy of repository owners.

The tool is intended for portfolio review, project discovery, learning, and
legitimate team research. Do not use repository information to impersonate,
target, manipulate, harass, or socially engineer people.

## Project structure

```text
github-crawler/
├── main.py
└── README.md
```

## License

No license has been specified for this project yet.
