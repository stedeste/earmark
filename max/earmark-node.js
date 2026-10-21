/*
    earmark-node.js
    Node for Max script to query Earmark database
    Uses sql.js (pure JavaScript SQLite)
    
    Setup in Max:
        [node.script earmark-node.js @autostart 1]
        
    Commands (send as messages):
        fields                          - list all fields
        listall                         - list all files
		count							- total number of files in database
        gt Quality 5                    - files where Quality > 5
        gte Quality 5                   - files where Quality >= 5
        lt Quality 5                    - files where Quality < 5
        lte Quality 5                   - files where Quality <= 5
        eq Type Interview               - files where Type = Interview
        between Quality 3 7             - files where Quality between 3-7
        contains TranscriptionFile mama - files where transcription contains "mama"
        hastag happy                    - files with tag "happy"
        search mama                     - search all text fields for "mama"
        searchmarkers mama              - search all marker labels for "mama"
        sort Quality asc                - all files sorted by Quality
        sort Quality desc               - all files sorted descending
        markers MyMarkers               - get all markers from field
        markers MyMarkers bird          - get markers containing "bird"
        stats Quality                   - statistics for a field
        stats tags                      - file-level tag statistics
        wordfreq Markers 50             - top 50 words in a field
        wordfreq Markers count          - total unique words in a field
        getfile 5                       - get all metadata for file index 5
		getfilepath 5					- get filepath for file index 5
        getfield 5 Quality              - get one field's value for file index 5
                                          (datalink fields also emit getfield_content)
        random                          - random file from last query
        randomfile                      - random file from all
        setdb /path/to/earmark.db       - change database path
        reload                          - re-read the database (after editing in Earmark)
*/

const Max = require('max-api');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Default database: where the Earmark app keeps them (Documents/Earmark).
// Send `setdb /path/to/other.db` to use a different one.
let DB_PATH = path.join(os.homedir(), 'Documents', 'Earmark', 'earmark.db');

let db = null;
let SQL = null;
let lastResults = [];

// Initialize sql.js and database
async function initDB() {
    try {
        const initSqlJs = require('sql.js');
        SQL = await initSqlJs();
        
        if (!fs.existsSync(DB_PATH)) {
            Max.post(`Database file not found: ${DB_PATH}`);
            return false;
        }
        
        const fileBuffer = fs.readFileSync(DB_PATH);
        db = new SQL.Database(fileBuffer);
        
        Max.post(`Connected to database: ${DB_PATH}`);
        return true;
    } catch (err) {
        Max.post(`Error connecting to database: ${err.message}`);
        return false;
    }
}

// Helper: Run query and return results as array of objects
function query(sql, params = []) {
    try {
        const stmt = db.prepare(sql);
        if (params.length > 0) {
            stmt.bind(params);
        }
        
        const results = [];
        while (stmt.step()) {
            const row = stmt.getAsObject();
            results.push(row);
        }
        stmt.free();
        return results;
    } catch (err) {
        Max.post(`Query error: ${err.message}`);
        return [];
    }
}

// Helper: Get all fields
function getFields() {
    const rows = query('SELECT id, name, type, min_val, max_val, choices FROM fields');
    return rows.map(r => ({
        id: r.id,
        name: r.name,
        type: r.type,
        min: r.min_val,
        max: r.max_val,
        choices: r.choices ? JSON.parse(r.choices) : null
    }));
}

// Helper: Get all files with their field values
function getFiles() {
    const files = query('SELECT id, filename, filepath, tags FROM files ORDER BY id');
    
    return files.map((f, index) => {
        const fileData = {
            index: index,
            id: f.id,
            filename: f.filename,
            filepath: f.filepath,
            tags: f.tags ? JSON.parse(f.tags) : []
        };
        
        // Get field values
        const values = query(`
            SELECT f.name, f.type, v.value, v.content 
            FROM field_values v 
            JOIN fields f ON v.field_id = f.id 
            WHERE v.file_id = ?
        `, [f.id]);
        
        values.forEach(v => {
            if (v.type === 'parameter') {
                fileData[v.name] = v.value ? parseFloat(v.value) : null;
            } else if (v.type === 'markers') {
                try {
                    const markers = JSON.parse(v.value || '[]');
                    fileData[v.name] = markers.map(m => ({
                        label: m.label,
                        start_ms: timeToMs(m.start),
                        end_ms: timeToMs(m.end)
                    }));
                } catch (e) {
                    fileData[v.name] = [];
                }
            } else if (v.type === 'tags') {
                try {
                    fileData[v.name] = JSON.parse(v.value || '[]');
                } catch (e) {
                    fileData[v.name] = [];
                }
            } else {
                fileData[v.name] = v.value || null;
            }
            
            // Store content for datalink fields
            if (v.type === 'datalink' && v.content) {
                fileData[v.name + '_content'] = v.content;
            }
        });
        
        return fileData;
    });
}

// Helper: Convert HH:MM:SS to milliseconds
function timeToMs(timeStr) {
    if (!timeStr) return 0;
    const parts = timeStr.split(':').map(Number);
    if (parts.length === 3) {
        return (parts[0] * 3600 + parts[1] * 60 + parts[2]) * 1000;
    }
    return 0;
}

// Helper: Output results
function outputResults(results, queryDesc) {
    lastResults = results;
    Max.post(`Query: ${queryDesc} → ${results.length} results`);
    
    results.forEach(f => {
        Max.outlet('file', f.index, f.filename);
    });
    Max.outlet('done', results.length);
}

// ============================================================
// COMMAND HANDLERS
// ============================================================

Max.addHandler('setdb', async (dbPath) => {
    DB_PATH = dbPath;
    await initDB();
});

Max.addHandler('reload', async () => {
    await initDB();
});

Max.addHandler('fields', () => {
    if (!db) { Max.post('Database not connected'); return; }
    
    const fields = getFields();
    fields.forEach(f => {
        let details = '';
        if (f.type === 'parameter') {
            details = `${f.min}-${f.max}`;
        } else if (f.type === 'choice' && f.choices) {
            details = f.choices.join(', ');
        }
        Max.outlet('field', f.name, f.type, details);
    });
    Max.outlet('fields_done', fields.length);
});

Max.addHandler('listall', () => {
    if (!db) { Max.post('Database not connected'); return; }
    
    const files = getFiles();
    outputResults(files, 'all files');
});

Max.addHandler('gt', (fieldName, value) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    value = parseFloat(value);
    const files = getFiles().filter(f => f[fieldName] !== null && f[fieldName] > value);
    outputResults(files, `${fieldName} > ${value}`);
});

Max.addHandler('gte', (fieldName, value) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    value = parseFloat(value);
    const files = getFiles().filter(f => f[fieldName] !== null && f[fieldName] >= value);
    outputResults(files, `${fieldName} >= ${value}`);
});

Max.addHandler('lt', (fieldName, value) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    value = parseFloat(value);
    const files = getFiles().filter(f => f[fieldName] !== null && f[fieldName] < value);
    outputResults(files, `${fieldName} < ${value}`);
});

Max.addHandler('lte', (fieldName, value) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    value = parseFloat(value);
    const files = getFiles().filter(f => f[fieldName] !== null && f[fieldName] <= value);
    outputResults(files, `${fieldName} <= ${value}`);
});

Max.addHandler('eq', (fieldName, value) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    const isNum = !isNaN(parseFloat(value));
    const files = getFiles().filter(f => {
        if (f[fieldName] === null || f[fieldName] === undefined) return false;
        if (isNum) {
            return parseFloat(f[fieldName]) === parseFloat(value);
        }
        return String(f[fieldName]).toLowerCase() === String(value).toLowerCase();
    });
    outputResults(files, `${fieldName} = ${value}`);
});

Max.addHandler('between', (fieldName, minVal, maxVal) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    minVal = parseFloat(minVal);
    maxVal = parseFloat(maxVal);
    const files = getFiles().filter(f => 
        f[fieldName] !== null && f[fieldName] >= minVal && f[fieldName] <= maxVal
    );
    outputResults(files, `${fieldName} between ${minVal}-${maxVal}`);
});

Max.addHandler('contains', (fieldName, searchText) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    searchText = String(searchText).toLowerCase();
    const files = getFiles().filter(f => {
        const val = f[fieldName];
        const content = f[fieldName + '_content'];
        
        if (val && String(val).toLowerCase().includes(searchText)) return true;
        if (content && String(content).toLowerCase().includes(searchText)) return true;
        return false;
    });
    outputResults(files, `${fieldName} contains "${searchText}"`);
});

Max.addHandler('hastag', (tagName) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    tagName = String(tagName).toLowerCase();
    const files = getFiles().filter(f => 
        f.tags && f.tags.some(t => String(t).toLowerCase().includes(tagName))
    );
    outputResults(files, `tag contains "${tagName}"`);
});

Max.addHandler('search', (searchText) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    searchText = String(searchText).toLowerCase();
    const files = getFiles().filter(f => {
        // Check filename
        if (f.filename.toLowerCase().includes(searchText)) return true;
        
        // Check tags
        if (f.tags && f.tags.some(t => String(t).toLowerCase().includes(searchText))) return true;
        
        // Check all string fields
        for (const key in f) {
            const val = f[key];
            if (typeof val === 'string' && val.toLowerCase().includes(searchText)) return true;
        }
        return false;
    });
    outputResults(files, `search "${searchText}"`);
});

Max.addHandler('searchmarkers', (searchText) => {
    if (!db) { Max.post('Database not connected'); return; }
    if (!searchText) { Max.post('Usage: searchmarkers <term>'); return; }
    
    searchText = String(searchText).toLowerCase();
    const markerFields = getFields().filter(f => f.type === 'markers');
    
    if (markerFields.length === 0) {
        Max.post('No marker fields found in database');
        return;
    }
    
    const files = getFiles();
    let count = 0;
    
    files.forEach(f => {
        markerFields.forEach(field => {
            const markers = f[field.name];
            if (markers && Array.isArray(markers)) {
                markers.forEach(m => {
                    if (m.label && String(m.label).toLowerCase().includes(searchText)) {
                        Max.outlet('marker', f.index, f.filename, m.label, m.start_ms, m.end_ms);
                        count++;
                    }
                });
            }
        });
    });
    
    Max.outlet('markers_done', count);
    Max.post(`Found ${count} markers matching "${searchText}"`);
});

Max.addHandler('sort', (fieldName, order = 'asc') => {
    if (!db) { Max.post('Database not connected'); return; }
    
    const files = getFiles();
    files.sort((a, b) => {
        const valA = a[fieldName] !== null ? a[fieldName] : -999999;
        const valB = b[fieldName] !== null ? b[fieldName] : -999999;
        
        if (order === 'desc') return valB - valA;
        return valA - valB;
    });
    outputResults(files, `sorted by ${fieldName} ${order}`);
});

Max.addHandler('markers', (fieldName, labelSearch = null) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    const files = getFiles();
    let count = 0;
    
    files.forEach(f => {
        const markers = f[fieldName];
        if (markers && Array.isArray(markers)) {
            markers.forEach(m => {
                if (labelSearch) {
                    if (!String(m.label).toLowerCase().includes(String(labelSearch).toLowerCase())) {
                        return;
                    }
                }
                Max.outlet('marker', f.index, f.filename, m.label, m.start_ms, m.end_ms);
                count++;
            });
        }
    });
    
    Max.outlet('markers_done', count);
    Max.post(`Found ${count} markers`);
});

Max.addHandler('getfile', (index) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    index = parseInt(index);
    const files = getFiles();
    const f = files.find(file => file.index === index);
    
    if (!f) {
        Max.post(`File not found at index ${index}`);
        return;
    }
    
    Max.post(`File ${index}: ${f.filename}`);
    
    for (const key in f) {
        const val = f[key];
        if (key.includes('_content')) {
            Max.outlet('data', key, '[indexed content]');
        } else if (Array.isArray(val)) {
            Max.outlet('data', key, `[array:${val.length}]`);
        } else {
            Max.outlet('data', key, val);
        }
    }
    Max.outlet('file_done', index);
});

Max.addHandler('getfield', (index, fieldName) => {
    if (!db) { Max.post('Database not connected'); return; }
    if (fieldName === undefined || fieldName === null || fieldName === '') {
        Max.post('Usage: getfield <fileindex> <fieldname>');
        return;
    }

    index = parseInt(index);
    const files = getFiles();
    const f = files.find(file => file.index === index);

    if (!f) {
        Max.post(`File not found at index ${index}`);
        return;
    }

    // Resolve canonical field name (case-insensitive). First try the registered
    // custom fields, then fall back to a key lookup on the file object so
    // built-ins like filename / filepath / tags also work.
    const fieldDef = getFields().find(fd => fd.name.toLowerCase() === String(fieldName).toLowerCase());
    let canonicalName = fieldDef ? fieldDef.name : null;
    if (!canonicalName) {
        const key = Object.keys(f).find(k => k.toLowerCase() === String(fieldName).toLowerCase());
        canonicalName = key || String(fieldName);
    }

    const val = f[canonicalName];
    let outVal;
    if (val === null || val === undefined) {
        outVal = '';
    } else if (Array.isArray(val) || typeof val === 'object') {
        // tags arrays, marker arrays, etc. → emit as one JSON string so the
        // message stays a single token in Max.
        outVal = JSON.stringify(val);
    } else {
        outVal = val;
    }

    Max.outlet('getfield', index, canonicalName, outVal);

    // Datalink fields also emit the indexed content as a separate message.
    if (fieldDef && fieldDef.type === 'datalink') {
        const content = f[canonicalName + '_content'];
        if (content !== undefined && content !== null && content !== '') {
            Max.outlet('getfield_content', index, canonicalName, content);
        }
    }

    Max.outlet('getfield_done', index, canonicalName);
});

Max.addHandler('getfilepath', (index) => {
    if (!db) { Max.post('Database not connected'); return; }
    
    index = parseInt(index);
    const files = getFiles();
    const f = files.find(file => file.index === index);
    
    if (!f) {
        Max.post(`File not found at index ${index}`);
        return;
    }
    
    Max.outlet('filepath', index, f.filepath);
});

Max.addHandler('random', () => {
    if (lastResults.length === 0) {
        Max.post('No results to pick from - run a query first');
        return;
    }
    
    const idx = Math.floor(Math.random() * lastResults.length);
    const f = lastResults[idx];
    Max.outlet('random', f.index, f.filename);
});

Max.addHandler('randomfile', () => {
    if (!db) { Max.post('Database not connected'); return; }
    
    const files = getFiles();
    if (files.length === 0) {
        Max.post('No files in database');
        return;
    }
    
    const idx = Math.floor(Math.random() * files.length);
    const f = files[idx];
    Max.outlet('random', f.index, f.filename);
});

Max.addHandler('stats', (fieldName) => {
    if (!db) { Max.post('Database not connected'); return; }
    if (!fieldName) { Max.post('Usage: stats <fieldname>'); return; }
    
    fieldName = String(fieldName);
    
    // Special case: file-level tags
    if (fieldName.toLowerCase() === 'tags') {
        const files = getFiles();
        const tagCounts = {};
        files.forEach(f => {
            if (f.tags) {
                f.tags.forEach(t => {
                    const key = t.toLowerCase();
                    if (!tagCounts[key]) tagCounts[key] = { tag: t, count: 0 };
                    tagCounts[key].count++;
                });
            }
        });
        const sorted = Object.values(tagCounts).sort((a, b) => b.count - a.count);
        Max.outlet('stats', 'field', 'tags', 'file_tags');
        Max.outlet('stats', 'total_files', files.length);
        Max.outlet('stats', 'unique_tags', sorted.length);
        sorted.forEach(t => {
            Max.outlet('stats', 'tag', t.tag, t.count);
        });
        Max.outlet('stats_done');
        Max.post(`Stats: tags — ${sorted.length} unique tags`);
        return;
    }
    
    const fields = getFields();
    const field = fields.find(f => f.name.toLowerCase() === fieldName.toLowerCase());
    if (!field) {
        Max.post(`Field "${fieldName}" not found`);
        return;
    }
    
    const files = getFiles();
    const totalFiles = files.length;
    
    Max.outlet('stats', 'field', field.name, field.type);
    Max.outlet('stats', 'total_files', totalFiles);
    
    if (field.type === 'parameter') {
        const values = [];
        files.forEach(f => {
            const v = f[field.name];
            if (v !== null && v !== undefined && !isNaN(v)) values.push(v);
        });
        
        if (values.length > 0) {
            values.sort((a, b) => a - b);
            const n = values.length;
            const sum = values.reduce((a, b) => a + b, 0);
            const mean = sum / n;
            const median = n % 2 === 1 ? values[Math.floor(n / 2)] : (values[n / 2 - 1] + values[n / 2]) / 2;
            const variance = values.reduce((a, v) => a + (v - mean) ** 2, 0) / n;
            const stdDev = Math.sqrt(variance);
            
            Max.outlet('stats', 'count', n);
            Max.outlet('stats', 'min', values[0]);
            Max.outlet('stats', 'max', values[n - 1]);
            Max.outlet('stats', 'mean', Math.round(mean * 100) / 100);
            Max.outlet('stats', 'median', Math.round(median * 100) / 100);
            Max.outlet('stats', 'std_dev', Math.round(stdDev * 100) / 100);
            Max.outlet('stats', 'sum', Math.round(sum * 100) / 100);
        } else {
            Max.outlet('stats', 'count', 0);
        }
        Max.outlet('stats', 'files_with_value', values.length);
        Max.outlet('stats', 'files_without_value', totalFiles - values.length);
        
    } else if (field.type === 'choice') {
        const counts = {};
        let total = 0;
        files.forEach(f => {
            const v = f[field.name];
            if (v) {
                counts[v] = (counts[v] || 0) + 1;
                total++;
            }
        });
        const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
        sorted.forEach(([val, count]) => {
            Max.outlet('stats', 'option', val, count, Math.round(count / total * 1000) / 10);
        });
        Max.outlet('stats', 'files_with_value', total);
        Max.outlet('stats', 'files_without_value', totalFiles - total);
        
    } else if (field.type === 'tags') {
        const tagCounts = {};
        let filesWithValue = 0;
        files.forEach(f => {
            const tags = f[field.name];
            if (tags && Array.isArray(tags) && tags.length > 0) {
                filesWithValue++;
                tags.forEach(t => {
                    const key = t.toLowerCase();
                    if (!tagCounts[key]) tagCounts[key] = { tag: t, count: 0 };
                    tagCounts[key].count++;
                });
            }
        });
        const sorted = Object.values(tagCounts).sort((a, b) => b.count - a.count);
        Max.outlet('stats', 'unique_tags', sorted.length);
        sorted.forEach(t => {
            Max.outlet('stats', 'tag', t.tag, t.count);
        });
        Max.outlet('stats', 'files_with_value', filesWithValue);
        Max.outlet('stats', 'files_without_value', totalFiles - filesWithValue);
        
    } else if (field.type === 'markers') {
        let totalMarkers = 0;
        let filesWithMarkers = 0;
        const colorCounts = {};
        files.forEach(f => {
            const markers = f[field.name];
            if (markers && Array.isArray(markers) && markers.length > 0) {
                filesWithMarkers++;
                totalMarkers += markers.length;
                markers.forEach(m => {
                    const color = m.color || 'Unknown';
                    colorCounts[color] = (colorCounts[color] || 0) + 1;
                });
            }
        });
        Max.outlet('stats', 'total_markers', totalMarkers);
        Max.outlet('stats', 'files_with_markers', filesWithMarkers);
        Max.outlet('stats', 'avg_per_file', filesWithMarkers ? Math.round(totalMarkers / filesWithMarkers * 10) / 10 : 0);
        const sortedColors = Object.entries(colorCounts).sort((a, b) => b[1] - a[1]);
        sortedColors.forEach(([color, count]) => {
            Max.outlet('stats', 'color', color, count);
        });
        Max.outlet('stats', 'files_with_value', filesWithMarkers);
        Max.outlet('stats', 'files_without_value', totalFiles - filesWithMarkers);
        
    } else {
        // text, datalink
        let filesWithValue = 0;
        files.forEach(f => {
            if (f[field.name]) filesWithValue++;
        });
        Max.outlet('stats', 'files_with_value', filesWithValue);
        Max.outlet('stats', 'files_without_value', totalFiles - filesWithValue);
    }
    
    Max.outlet('stats_done');
    Max.post(`Stats: ${field.name} (${field.type})`);
});

Max.addHandler('wordfreq', (fieldName, limitOrCount = '20') => {
    if (!db) { Max.post('Database not connected'); return; }
    if (!fieldName) { Max.post('Usage: wordfreq <fieldname> [limit] or wordfreq <fieldname> count'); return; }
    
    fieldName = String(fieldName);
    const fields = getFields();
    const field = fields.find(f => f.name.toLowerCase() === fieldName.toLowerCase());
    if (!field) {
        Max.post(`Field "${fieldName}" not found`);
        return;
    }
    
    const countOnly = String(limitOrCount).toLowerCase() === 'count';
    const limit = countOnly ? Infinity : parseInt(limitOrCount) || 20;
    
    const files = getFiles();
    const wordCounts = {};
    
    files.forEach(f => {
        let texts = [];
        
        if (field.type === 'markers') {
            const markers = f[field.name];
            if (markers && Array.isArray(markers)) {
                texts = markers.map(m => m.label || '');
            }
        } else if (field.type === 'tags') {
            const tags = f[field.name];
            if (tags && Array.isArray(tags)) {
                texts = tags;
            }
        } else if (field.type === 'datalink') {
            const content = f[field.name + '_content'];
            if (content) texts = [content];
        } else {
            const val = f[field.name];
            if (val && typeof val === 'string') texts = [val];
        }
        
        texts.forEach(text => {
            if (!text) return;
            const words = text.toLowerCase().match(/[a-zA-Z\u00C0-\u00FF\u0100-\u024F]+/g);
            if (words) {
                words.forEach(w => {
                    wordCounts[w] = (wordCounts[w] || 0) + 1;
                });
            }
        });
    });
    
    const uniqueCount = Object.keys(wordCounts).length;
    
    if (countOnly) {
        Max.outlet('wordfreq', 'field', field.name, field.type);
        Max.outlet('wordfreq', 'unique_words', uniqueCount);
        Max.outlet('wordfreq_done');
        Max.post(`Wordfreq: ${field.name} — ${uniqueCount} unique words`);
        return;
    }
    
    const sorted = Object.entries(wordCounts)
        .sort((a, b) => b[1] - a[1])
        .slice(0, limit);
    
    Max.outlet('wordfreq', 'field', field.name, field.type);
    Max.outlet('wordfreq', 'unique_words', uniqueCount);
    Max.outlet('wordfreq', 'showing', sorted.length);
    
    sorted.forEach(([word, count], i) => {
        Max.outlet('wordfreq', 'word', i + 1, word, count);
    });
    
    Max.outlet('wordfreq_done');
    Max.post(`Wordfreq: ${field.name} — top ${sorted.length} of ${uniqueCount} unique words`);
});

Max.addHandler('count', () => {
    if (!db) { Max.post('Database not connected'); return; }
    
    const files = getFiles();
    Max.outlet('count', files.length);
    Max.post(`Database has ${files.length} files`);
});

// Initialize on load
initDB();