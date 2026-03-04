let entries = [];

function normalize(text) {
    return text.trim().toLowerCase().replace(/\s+/g, " ");
}

function formatEntry(entry) {
    const media = entry.media;

    if (entry.entry_type === "profile") {
        return `${media.videos} Videos, ${media.images} Images, ${media.total} Total — ${entry.source}`;
    } else {
        return `${media.total} Total Files — ${entry.source}`;
    }
}

function groupByName(results) {
    const groups = {};

    results.forEach(entry => {
        if (!groups[entry.normalized_name]) {
            groups[entry.normalized_name] = [];
        }
        groups[entry.normalized_name].push(entry);
    });

    return groups;
}

function renderResults(results) {
    const resultsDiv = document.getElementById("results");
    const statsDiv = document.getElementById("stats");

    resultsDiv.innerHTML = "";

    if (results.length === 0) {
        statsDiv.textContent = "No results.";
        return;
    }

    statsDiv.textContent = `${results.length} entries found`;

    const grouped = groupByName(results);

    Object.keys(grouped).forEach(name => {
        const groupDiv = document.createElement("div");
        groupDiv.className = "creator-group";

        const title = document.createElement("div");
        title.className = "creator-name";
        title.textContent = grouped[name][0].display_name;
        groupDiv.appendChild(title);

        grouped[name].forEach(entry => {
            const entryDiv = document.createElement("div");
            entryDiv.className = "entry";

            const link = document.createElement("a");
            link.href = entry.url;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            link.textContent = formatEntry(entry);

            entryDiv.appendChild(link);
            groupDiv.appendChild(entryDiv);
        });

        resultsDiv.appendChild(groupDiv);
    });
}

function handleSearch() {
    const query = normalize(document.getElementById("searchInput").value);

    if (query === "") {
        document.getElementById("results").innerHTML = "";
        document.getElementById("stats").textContent = "";
        return;
    }

    const filtered = entries.filter(entry =>
        entry.normalized_name.includes(query)
    );

    renderResults(filtered);
}

async function init() {
    const response = await fetch("index.json");
    const data = await response.json();
    entries = data.entries;

    document.getElementById("searchInput")
        .addEventListener("input", handleSearch);
}

init();
