/*
    earmark-query.js
    Max/MSP JavaScript for querying Earmark database
    
    Setup in Max:
        [loadmess read earmark-max.json]
              |
              v
        [dict earmark]
        
        [js earmark-query.js]
        
    Outlets:
        0 - Main results (lists, counts, data)
        1 - Status messages / errors
*/

inlets = 1;
outlets = 2;

var dictName = "earmark";

// ============================================================
// UTILITY FUNCTIONS
// ============================================================

function setdict(name) {
    // Change which dict to query
    dictName = name;
    outlet(1, "dict set to: " + name);
}

function getDict() {
    var d = new Dict(dictName);
    if (!d.name) {
        outlet(1, "error", "dict '" + dictName + "' not found");
        return null;
    }
    return d;
}

function getFiles() {
    var d = getDict();
    if (!d) return [];
    
    var files = d.get("files");
    if (!files) {
        outlet(1, "error", "no files in dict");
        return [];
    }
    
    return files;
}

function getFileCount() {
    var d = getDict();
    if (!d) return 0;
    
    var files = d.get("files");
    if (!files) return 0;
    
    return files.length;
}

function getFieldsInfo() {
    var d = getDict();
    if (!d) return {};
    return d.get("fields");
}


// ============================================================
// 1. GET FIELDS INFO
// ============================================================

function fields() {
    // Output all field definitions
    // Format: [name, type, range/choices]
    
    var d = getDict();
    if (!d) return;
    
    var fieldsDict = d.get("fields");
    if (!fieldsDict) {
        outlet(1, "error", "no fields found");
        return;
    }
    
    var keys = fieldsDict.getkeys();
    if (!keys) {
        outlet(1, "error", "no field keys found");
        return;
    }
    
    // Handle single key (not array)
    if (typeof keys === "string") {
        keys = [keys];
    }
    
    outlet(1, "fields:", keys.length);
    
    for (var i = 0; i < keys.length; i++) {
        var name = keys[i];
        var info = fieldsDict.get(name);
        var type = info.type;
        
        var details = "";
        if (type === "parameter") {
            var minVal = info.min;
            var maxVal = info.max;
            details = minVal + "-" + maxVal;
        } else if (type === "choice") {
            var choices = info.choices;
            if (choices) {
                // choices is already an array
                details = choices.join(", ");
            }
        }
        
        // Output: fieldname type details
        outlet(0, "field", name, type, details);
    }
    
    outlet(0, "fields_done", keys.length);
}


// ============================================================
// 2. QUERY FUNCTIONS
// ============================================================

// --- Get all files ---
function all() {
    var files = getFiles();
    var count = files.length;
    outlet(1, "found", count, "files");
    
    for (var i = 0; i < count; i++) {
        var f = files[i];
        outlet(0, "file", f.index, f.filename);
    }
    outlet(0, "done", count);
}

// --- Sort files by field ---
function sort(fieldName, order) {
    // sort H asc
    // sort H desc
    order = order || "asc";
    
    var files = getFiles();
    var count = files.length;
    
    // Get values and sort
    var sortable = [];
    for (var i = 0; i < count; i++) {
        var f = files[i];
        var val = f[fieldName];
        // Handle null/undefined
        if (val === null || val === undefined) val = -999999;
        sortable.push({
            index: f.index,
            filename: f.filename,
            value: val
        });
    }
    
    sortable.sort(function(a, b) {
        if (order === "desc") {
            return b.value - a.value;
        }
        return a.value - b.value;
    });
    
    outlet(1, "sorted by", fieldName, order);
    
    for (var i = 0; i < sortable.length; i++) {
        outlet(0, "file", sortable[i].index, sortable[i].filename, sortable[i].value);
    }
    outlet(0, "done", sortable.length);
}

// --- Filter: field > value ---
function gt(fieldName, value) {
    // gt H 5  → files where H > 5
    value = parseFloat(value);
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var val = f[fieldName];
        if (val !== null && val !== undefined && val > value) {
            results.push(f);
        }
    }
    
    outputResults(results, fieldName + " > " + value);
}

// --- Filter: field >= value ---
function gte(fieldName, value) {
    // gte H 5  → files where H >= 5
    value = parseFloat(value);
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var val = f[fieldName];
        if (val !== null && val !== undefined && val >= value) {
            results.push(f);
        }
    }
    
    outputResults(results, fieldName + " >= " + value);
}

// --- Filter: field < value ---
function lt(fieldName, value) {
    // lt H 5  → files where H < 5
    value = parseFloat(value);
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var val = f[fieldName];
        if (val !== null && val !== undefined && val < value) {
            results.push(f);
        }
    }
    
    outputResults(results, fieldName + " < " + value);
}

// --- Filter: field <= value ---
function lte(fieldName, value) {
    // lte H 5  → files where H <= 5
    value = parseFloat(value);
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var val = f[fieldName];
        if (val !== null && val !== undefined && val <= value) {
            results.push(f);
        }
    }
    
    outputResults(results, fieldName + " <= " + value);
}

// --- Filter: field == value ---
function eq(fieldName, value) {
    // eq Language English → files where Language == "English"
    var files = getFiles();
    var results = [];
    var isNum = !isNaN(parseFloat(value));
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var val = f[fieldName];
        
        if (isNum) {
            if (val !== null && val !== undefined && parseFloat(val) === parseFloat(value)) {
                results.push(f);
            }
        } else {
            if (val !== null && val !== undefined && String(val).toLowerCase() === String(value).toLowerCase()) {
                results.push(f);
            }
        }
    }
    
    outputResults(results, fieldName + " = " + value);
}

// --- Filter: field between min and max ---
function between(fieldName, minVal, maxVal) {
    // between H 5 10 → files where H >= 5 and H <= 10
    minVal = parseFloat(minVal);
    maxVal = parseFloat(maxVal);
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var val = f[fieldName];
        if (val !== null && val !== undefined && val >= minVal && val <= maxVal) {
            results.push(f);
        }
    }
    
    outputResults(results, fieldName + " between " + minVal + "-" + maxVal);
}

// --- Filter: field contains text ---
function contains(fieldName, searchText) {
    // contains Transcription mama → files where Transcription contains "mama"
    // Also searches _content for datalink fields
    
    searchText = String(searchText).toLowerCase();
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var val = f[fieldName];
        var content = f[fieldName + "_content"];
        
        var found = false;
        
        if (val !== null && val !== undefined && String(val).toLowerCase().indexOf(searchText) !== -1) {
            found = true;
        }
        if (content !== null && content !== undefined && String(content).toLowerCase().indexOf(searchText) !== -1) {
            found = true;
        }
        
        if (found) {
            results.push(f);
        }
    }
    
    outputResults(results, fieldName + " contains '" + searchText + "'");
}

// --- Filter: has tag ---
function hastag(tagName) {
    // hastag happy → files with tag "happy"
    tagName = String(tagName).toLowerCase();
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var tags = f.tags;
        
        if (tags && tags.length > 0) {
            for (var j = 0; j < tags.length; j++) {
                if (String(tags[j]).toLowerCase().indexOf(tagName) !== -1) {
                    results.push(f);
                    break;
                }
            }
        }
    }
    
    outputResults(results, "tag contains '" + tagName + "'");
}

// --- Search all text fields ---
function search(searchText) {
    // search mama → search all text/_content fields for "mama"
    searchText = String(searchText).toLowerCase();
    var files = getFiles();
    var results = [];
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var found = false;
        
        // Check filename
        if (f.filename && String(f.filename).toLowerCase().indexOf(searchText) !== -1) {
            found = true;
        }
        
        // Check tags
        var tags = f.tags;
        if (tags && tags.length > 0 && !found) {
            for (var j = 0; j < tags.length; j++) {
                if (String(tags[j]).toLowerCase().indexOf(searchText) !== -1) {
                    found = true;
                    break;
                }
            }
        }
        
        // Check all string properties
        if (!found) {
            for (var key in f) {
                if (f.hasOwnProperty(key)) {
                    var val = f[key];
                    if (typeof val === "string" && val.toLowerCase().indexOf(searchText) !== -1) {
                        found = true;
                        break;
                    }
                }
            }
        }
        
        if (found) {
            results.push(f);
        }
    }
    
    outputResults(results, "search '" + searchText + "'");
}


// ============================================================
// 3. MARKER FUNCTIONS
// ============================================================

function markers(fieldName, labelSearch) {
    // markers BirdCalls bicycle → find markers containing "bicycle"
    // Outputs: index, filename, marker_label, start_sec, end_sec
    
    labelSearch = labelSearch ? String(labelSearch).toLowerCase() : null;
    var files = getFiles();
    var count = 0;
    
    outlet(1, "searching markers in", fieldName);
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        var markersData = f[fieldName];
        
        if (markersData && markersData.length > 0) {
            for (var j = 0; j < markersData.length; j++) {
                var m = markersData[j];
                var label = m.label;
                var startSec = m.start_sec;
                var endSec = m.end_sec;
                
                // Filter by label if search provided
                if (labelSearch) {
                    if (String(label).toLowerCase().indexOf(labelSearch) === -1) {
                        continue;
                    }
                }
                
                outlet(0, "marker", f.index, f.filename, label, startSec, endSec);
                count++;
            }
        }
    }
    
    outlet(0, "markers_done", count);
    outlet(1, "found", count, "markers");
}

function allmarkers(fieldName) {
    // allmarkers BirdCalls → get all markers from this field
    markers(fieldName, null);
}


// ============================================================
// 4. GET SPECIFIC FILE DATA
// ============================================================

function getfile(index) {
    // getfile 5 → get all metadata for file at index 5
    index = parseInt(index);
    var files = getFiles();
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        if (f.index === index) {
            outlet(1, "file", index, f.filename);
            
            for (var key in f) {
                if (f.hasOwnProperty(key)) {
                    var val = f[key];
                    
                    // Skip content fields (too long)
                    if (key.indexOf("_content") !== -1) {
                        outlet(0, "data", key, "[indexed content]");
                    } else if (Array.isArray(val)) {
                        outlet(0, "data", key, "[array:" + val.length + "]");
                    } else {
                        outlet(0, "data", key, val);
                    }
                }
            }
            outlet(0, "file_done", index);
            return;
        }
    }
    
    outlet(1, "error", "file not found at index", index);
}

function getfield(index, fieldName) {
    // getfield 5 H → get H value for file at index 5
    index = parseInt(index);
    var files = getFiles();
    
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        if (f.index === index) {
            var val = f[fieldName];
            outlet(0, "value", index, fieldName, val);
            return;
        }
    }
    
    outlet(1, "error", "file not found at index", index);
}


// ============================================================
// 5. RANDOM SELECTION
// ============================================================

var lastResults = [];

function random() {
    // random → pick random from last query results
    if (lastResults.length === 0) {
        outlet(1, "error", "no results to pick from - run a query first");
        return;
    }
    
    var idx = Math.floor(Math.random() * lastResults.length);
    var f = lastResults[idx];
    
    outlet(0, "random", f.index, f.filename);
}

function randomfile() {
    // randomfile → pick any random file
    var files = getFiles();
    if (files.length === 0) {
        outlet(1, "error", "no files in database");
        return;
    }
    
    var idx = Math.floor(Math.random() * files.length);
    var f = files[idx];
    
    outlet(0, "random", f.index, f.filename);
}


// ============================================================
// HELPER: Output results
// ============================================================

function outputResults(results, queryDesc) {
    lastResults = results;
    outlet(1, "query:", queryDesc, "→", results.length, "results");
    
    for (var i = 0; i < results.length; i++) {
        var f = results[i];
        outlet(0, "file", f.index, f.filename);
    }
    
    outlet(0, "done", results.length);
}


// ============================================================
// HELP
// ============================================================

function help() {
    post("\n");
    post("=== earmark-query.js ===\n");
    post("\n");
    post("SETUP:\n");
    post("  setdict <name>      - set dict name (default: earmark)\n");
    post("\n");
    post("GET INFO:\n");
    post("  fields              - list all fields with types\n");
    post("  all                 - list all files\n");
    post("  getfile <idx>       - get all metadata for file\n");
    post("  getfield <idx> <f>  - get specific field value\n");
    post("\n");
    post("QUERIES:\n");
    post("  gt <field> <val>        - field > value\n");
    post("  gte <field> <val>       - field >= value\n");
    post("  lt <field> <val>        - field < value\n");
    post("  lte <field> <val>       - field <= value\n");
    post("  eq <field> <val>        - field == value\n");
    post("  between <f> <min> <max> - field between min-max\n");
    post("  contains <field> <txt>  - field contains text\n");
    post("  hastag <tag>            - has tag\n");
    post("  search <text>           - search all text fields\n");
    post("  sort <field> [asc|desc] - sort by field\n");
    post("\n");
    post("MARKERS:\n");
    post("  markers <field> <label> - find markers by label\n");
    post("  allmarkers <field>      - get all markers\n");
    post("\n");
    post("RANDOM:\n");
    post("  random              - random from last results\n");
    post("  randomfile          - random from all files\n");
    post("\n");
}