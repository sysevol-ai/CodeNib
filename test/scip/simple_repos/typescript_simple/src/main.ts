// SPDX-FileCopyrightText: 2025-2026 CodeNib Contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Main entry point that uses math functions
 */

import { add, multiply, subtract, Calculator } from './math';

// Use the add function multiple times
const sum1 = add(5, 3);
const sum2 = add(10, 20);
const sum3 = add(sum1, sum2);

// Use multiply function
const product = multiply(4, 5);
const result = multiply(product, 2);

// Use subtract function
const diff = subtract(100, 50);

// Use Calculator class
const calc = new Calculator("MyCalc");
const calcResult = calc.calculate(15, 25);

// Reference add again
function wrapper(x: number, y: number): number {
    return add(x, y);
}

console.log("Sum:", sum3);
console.log("Product:", result);
console.log("Difference:", diff);
console.log("Calculator result:", calcResult);
console.log("Wrapper result:", wrapper(1, 2));
