// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Math utility functions
 */

export function add(a: number, b: number): number {
    return a + b;
}

export function multiply(a: number, b: number): number {
    return a * b;
}

export function subtract(a: number, b: number): number {
    return a - b;
}

export class Calculator {
    constructor(public name: string) {}

    calculate(x: number, y: number): number {
        return add(x, y);
    }
}
